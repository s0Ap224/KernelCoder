"""Host-side contract tests; these are NOT evidence of OS containment."""

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kernelcoder.agent import Agent
from kernelcoder.demo import ScriptedLLM
from kernelcoder.llm import LLMResponse, ToolCall
from kernelcoder.permissions import Permission
from kernelcoder.sandbox import BoundedOutput, SandboxRunner, classify
from kernelcoder.tools import ALL_TOOLS
from kernelcoder.tools.bash import BashTool
from kernelcoder.workspace import SandboxWorkspace, WorkspaceError, _linked


@pytest.fixture
def ws(tmp_path):
    project = tmp_path / "project space 中文"
    project.mkdir()
    (project / "code.py").write_text("value = 1\n", encoding="utf-8")
    return SandboxWorkspace.create(project, tmp_path / "store space 中文")


def test_default_bash_never_falls_back(monkeypatch):
    host = Mock(side_effect=AssertionError("host shell reached"))
    monkeypatch.setattr("kernelcoder.tools.bash.run_shell", host)
    assert "No host fallback" in BashTool().execute("echo hi")
    host.assert_not_called()


def test_missing_key_refuses_bash_with_a_workspace(ws, monkeypatch):
    host = Mock(side_effect=AssertionError("host shell reached"))
    monkeypatch.setattr("kernelcoder.tools.bash.run_shell", host)
    output = BashTool(ws).execute("echo hi")
    assert "runtime_error" in output and "E2B_API_KEY" in output
    assert ws.meta["active"] is None
    host.assert_not_called()


@pytest.mark.parametrize("args", [["--sandbox-check"], ["--demo"]])
def test_missing_e2b_key_fails_before_model_config(monkeypatch, capsys, args):
    from kernelcoder import cli
    monkeypatch.setattr("sys.argv", ["kernelcoder", *args])
    monkeypatch.setattr(cli.Config, "from_env", Mock(side_effect=AssertionError("credentials loaded")))
    monkeypatch.setattr(cli, "LLM", Mock(side_effect=AssertionError("LLM constructed")))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "E2B_API_KEY" in capsys.readouterr().out


@pytest.mark.parametrize("args", [["--sandbox", "docker"], ["--sandbox-image", "sha256:old"]])
def test_removed_docker_options_are_rejected(monkeypatch, args):
    from kernelcoder import cli
    monkeypatch.setattr("sys.argv", ["kernelcoder", *args])
    with pytest.raises(SystemExit) as exc:
        cli._parse_args()
    assert exc.value.code == 2


def test_cleanup_cannot_unlock_unverified_legacy_execution(ws):
    ws.meta.update(active="legacy-container", image="sha256:old", endpoint="unix:///old-daemon")
    ws._save()
    with pytest.raises(WorkspaceError, match="cannot confirm cleanup"):
        SandboxRunner().cleanup(ws)
    reopened = SandboxWorkspace.open(ws.id, ws.directory.parent)
    assert reopened.meta["active"] == "legacy-container"
    for action in (reopened.review, reopened.apply, reopened.discard):
        with pytest.raises(WorkspaceError, match="Unclean"):
            action()


def test_cli_can_review_and_apply_copy_without_backend(ws, monkeypatch, capsys):
    from kernelcoder import cli
    (ws.tree / "code.py").write_text("value = 2\n")
    monkeypatch.setattr(cli.Config, "from_env", Mock(side_effect=AssertionError("credentials loaded")))
    for action in ("status", "diff", "apply"):
        monkeypatch.setattr("sys.argv", ["kernelcoder", "--sandbox-session", ws.id,
                                         "--sandbox-store", str(ws.directory.parent), "--sandbox-action", action])
        cli.main()
    assert "Applied 1" in capsys.readouterr().out
    assert (ws.original / "code.py").read_text() == "value = 2\n"


def test_clean_workspace_cleanup_does_not_claim_remote_deletion(ws):
    assert "No backend cleanup performed" in SandboxRunner().cleanup(ws)


def test_unsafe_cannot_resume_a_sandbox_conversation(ws, monkeypatch):
    from kernelcoder import cli
    monkeypatch.setattr("sys.argv", ["kernelcoder", "--sandbox", "unsafe", "--resume", "conversation"])
    monkeypatch.setattr(cli, "session_sandbox_id", lambda sid: ws.id)
    monkeypatch.setattr(cli.Config, "from_env", lambda: SimpleNamespace(
        model="test", api_key="test", base_url=None, provider="openai", temperature=0,
        max_tokens=100, max_context_tokens=1000))
    monkeypatch.setattr(cli, "LLM", lambda **kw: ScriptedLLM([]))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("value", [0, -1, 601, "120", True, 1.5])
def test_timeout_validation(ws, value):
    assert SandboxRunner().run(ws, "true", value).status == "policy_denied"


@pytest.mark.parametrize("command", ["vim file.py", "less file", "python", "bash"])
def test_obvious_interactive_commands_are_refused(ws, command):
    assert SandboxRunner().run(ws, command).status == "policy_denied"


def test_complex_shell_is_unknown_not_privileged():
    assert classify("echo yes && python") == "unknown"
    assert classify("python -c 'print(1)'") == "unknown"  # deliberately conservative
    assert classify("python -V") == "noninteractive"


def test_output_buffer_is_bounded_and_keeps_tail():
    out = BoundedOutput()
    out.add(b"HEAD")
    for _ in range(100):
        out.add(b"x" * 8192)
    out.add(b"TAIL")
    assert len(out.head) + len(out.tail) <= 9000
    assert out.text().startswith("HEAD")
    assert out.text().endswith("TAIL")
    assert "truncated" in out.text()


def test_copy_excludes_secrets_and_contains_uncommitted_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in [".env", ".env.local", "id_rsa", "key.pem", "untracked.txt", "private/custom.txt"]:
        p = root / name
        p.parent.mkdir(exist_ok=True)
        p.write_text("data")
    workspace = SandboxWorkspace.create(root, tmp_path / "store", ["private"])
    assert sorted(workspace.scan()) == ["untracked.txt"]
    assert ".env" in workspace.status()
    assert workspace.baseline.parent != workspace.tree


@pytest.mark.parametrize("value", ["../outside", "/etc/passwd", "/workspace/../secret", "C:escape", "a:stream", "NUL", "a.", "//server/share"])
def test_paths_cannot_escape(ws, value):
    with pytest.raises(WorkspaceError):
        ws.path(value)


def test_project_absolute_and_container_paths_agree(ws):
    expected = ws.tree / "code.py"
    assert ws.path(str(ws.original / "code.py")) == expected
    assert ws.path("/workspace/code.py") == expected
    assert ws.path("code.py") == expected


def test_links_and_reparse_points_are_rejected(ws, tmp_path):
    outside = tmp_path / "sentinel"
    outside.write_text("SECRET")
    try:
        os.link(outside, ws.tree / "hardlink")
    except OSError:
        pytest.skip("OS sandbox prevents hard-link creation; requires a host with hard-link privileges")
    with pytest.raises(WorkspaceError, match="Hard link"):
        ws.path("hardlink")
    with pytest.raises(WorkspaceError):
        ws.review()
    assert _linked(SimpleNamespace(st_mode=0, st_file_attributes=0x400))


def test_symlink_cannot_read_outside(ws, tmp_path):
    outside = tmp_path / "sentinel"
    outside.write_text("SECRET")
    try:
        (ws.tree / "link").symlink_to(outside)
    except OSError:
        pytest.skip("OS does not grant symlink creation; run on a host with symlink privileges")
    with pytest.raises(WorkspaceError, match="Link"):
        ws.path("link")


def test_file_tools_share_copy_undo_and_cannot_read_host(ws):
    agent = Agent(ScriptedLLM([]), tools=[type(t)() for t in ALL_TOOLS], workspace=ws)
    def call(name, **kwargs):
        return agent._exec_tool(ToolCall(id="1", name=name, arguments=kwargs))
    assert "Edited" in call("edit_file", file_path="code.py", old_string="1", new_string="2")
    assert "value = 2" in call("read_file", file_path="/workspace/code.py")
    assert "/workspace/code.py" in call("glob", pattern="**/*.py")
    assert "value = 2" in call("grep", pattern="value", path=".")
    assert "outside" in call("read_file", file_path=str(ws.directory / "meta.json"))
    assert (ws.original / "code.py").read_text() == "value = 1\n"
    assert "Restored" in ws.undo()
    assert "value = 1" in (ws.tree / "code.py").read_text()


def test_review_apply_and_resume(ws):
    (ws.tree / "code.py").write_text("value = 2\n")
    (ws.tree / "new.txt").write_text("new")
    with pytest.raises(WorkspaceError, match="Review"):
        ws.apply()
    assert "ADD new.txt" in ws.review()
    resumed = SandboxWorkspace.open(ws.id, ws.directory.parent)
    assert "Applied 2" in resumed.apply()
    assert (ws.original / "code.py").read_text() == "value = 2\n"
    logs = list(resumed.directory.glob("backups/*/journal.json"))
    assert json.loads(logs[0].read_text())["status"] == "applied"
    with pytest.raises(WorkspaceError, match="applied"):
        resumed.review()


def test_delete_and_discard(ws):
    (ws.tree / "code.py").unlink()
    assert "DELETE code.py" in ws.review()
    ws.discard()
    assert not ws.tree.exists()
    assert (ws.original / "code.py").exists()


def test_apply_rejects_original_conflict_before_any_write(ws):
    (ws.tree / "code.py").write_text("candidate")
    (ws.tree / "a_new").write_text("candidate")
    ws.review()
    (ws.original / "code.py").write_text("user edit")
    with pytest.raises(WorkspaceError, match="conflict"):
        ws.apply()
    assert not (ws.original / "a_new").exists()
    assert (ws.original / "code.py").read_text() == "user edit"


def test_apply_rejects_changed_review_or_frozen_candidate(ws):
    (ws.tree / "code.py").write_text("candidate")
    ws.review()
    (ws.tree / "new").write_text("unreviewed")
    with pytest.raises(WorkspaceError, match="since review"):
        ws.apply()
    (ws.tree / "new").unlink()
    frozen = ws.directory / "reviews" / ws.meta["review"]["id"] / "code.py"
    frozen.write_text("tamper")
    with pytest.raises(WorkspaceError, match="hash mismatch"):
        ws.apply()


def test_apply_rolls_back_and_logs_failure(ws, monkeypatch):
    import kernelcoder.workspace as module
    (ws.tree / "code.py").write_text("candidate")
    (ws.tree / "z_new").write_text("new")
    ws.review()
    real = module.atomic_write
    def fail_second(path, data, mode=0o644):
        if path == ws.original / "z_new":
            raise OSError("simulated disk failure")
        return real(path, data, mode)
    monkeypatch.setattr(module, "atomic_write", fail_second)
    with pytest.raises(WorkspaceError, match="Restored code.py"):
        ws.apply()
    assert (ws.original / "code.py").read_text() == "value = 1\n"
    assert not (ws.original / "z_new").exists()


def test_busy_session_rejects_second_process_handle(ws):
    other = SandboxWorkspace.open(ws.id, ws.directory.parent)
    with ws.guard(), pytest.raises(WorkspaceError, match="busy"), other.guard():
        pass


def test_subagent_inherits_workspace_and_hooks_disabled(ws, monkeypatch):
    import kernelcoder.agent as module
    captured = []
    parent = Agent(ScriptedLLM([]), tools=[type(t)() for t in ALL_TOOLS], workspace=ws, hooks=Mock())
    real = module.Agent
    def make(**kwargs):
        child = real(**kwargs)
        captured.append(child)
        monkeypatch.setattr(child, "chat", lambda task: "done")
        return child
    monkeypatch.setattr(module, "Agent", make)
    parent._tool_by_name["agent"].execute("task")
    assert captured[0].workspace is ws
    assert captured[0].hooks is None
    assert "Bash runs remotely in E2B" in captured[0]._system and "/workspace" in captured[0]._system


def test_yes_and_model_arguments_cannot_enable_unsafe(ws):
    agent = Agent(ScriptedLLM([]), tools=[BashTool(ws)], workspace=ws, permission=Permission(allow_all=True))
    tc = ToolCall(id="1", name="bash", arguments={"command": "true", "unsafe": True})
    assert "bad arguments" in agent._exec_tool(tc)
    assert (ws.original / "code.py").read_text() == "value = 1\n"


def test_cli_check_does_not_need_config_or_api_key(monkeypatch, capsys):
    from kernelcoder import cli
    monkeypatch.setattr("sys.argv", ["kernelcoder", "--sandbox-check"])
    monkeypatch.setattr(cli.Config, "from_env", Mock(side_effect=AssertionError("should not load credentials")))
    monkeypatch.setattr(cli.SandboxRunner, "probe", lambda self: "ready")
    cli.main()
    assert "ready" in capsys.readouterr().out


def test_cli_defaults_to_pending_e2b(monkeypatch):
    from kernelcoder.cli import _parse_args
    monkeypatch.setattr("sys.argv", ["kernelcoder", "--yes"])
    assert _parse_args().sandbox == "e2b"


def test_agent_batch_observes_edits_in_order(ws):
    agent = Agent(ScriptedLLM([]), tools=[type(t)() for t in ALL_TOOLS], workspace=ws)
    result = agent._exec_tools_parallel([
        ToolCall(id="1", name="write_file", arguments={"file_path": "created", "content": "new content"}),
        ToolCall(id="2", name="read_file", arguments={"file_path": "created"}),
    ])
    assert "new content" in result[1]


def test_mcp_cannot_be_registered_in_sandbox(ws):
    tool = SimpleNamespace(name="mcp__host_exec")
    with pytest.raises(ValueError, match="MCP"):
        Agent(ScriptedLLM([]), tools=[tool], workspace=ws)


def test_cli_yes_leaves_original_and_persists_copy(ws, monkeypatch):
    from kernelcoder import cli
    monkeypatch.chdir(ws.original)
    monkeypatch.setattr("kernelcoder.session.SESSIONS_DIR", ws.directory.parent / "conversations")
    monkeypatch.setattr("sys.argv", ["kernelcoder", "--yes", "-p", "change code",
                                     "--sandbox-session", ws.id, "--sandbox-store", str(ws.directory.parent)])
    monkeypatch.setattr(cli.Config, "from_env", lambda: SimpleNamespace(
        model="test", api_key="test", base_url=None, provider="openai", temperature=0,
        max_tokens=100, max_context_tokens=1000))
    monkeypatch.setattr(cli, "LLM", lambda **kw: ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall(id="1", name="write_file", arguments={"file_path": "code.py", "content": "new"})]),
        LLMResponse(content="done")]))
    monkeypatch.setattr(cli.SandboxRunner, "check", lambda self: "ready")
    monkeypatch.setattr(cli, "load_hooks", Mock(side_effect=AssertionError("hooks loaded")))
    monkeypatch.setattr(cli, "load_mcp_tools", Mock(side_effect=AssertionError("MCP loaded")))
    cli.main()
    assert (ws.original / "code.py").read_text() == "value = 1\n"
    assert (ws.tree / "code.py").read_text() == "new"
    reopened = SandboxWorkspace.open(ws.id, ws.directory.parent)
    assert reopened.meta["review"]
    assert reopened.meta["state"] == "open"


def test_reused_agent_tool_cannot_take_another_parents_policy(ws):
    from kernelcoder.tools.agent import AgentTool
    shared = AgentTool()
    sandboxed = Agent(ScriptedLLM([]), tools=[shared], workspace=ws)
    unsafe = Agent(ScriptedLLM([]), tools=[shared], hooks=Mock())
    assert sandboxed._tool_by_name["agent"]._parent_agent is sandboxed
    assert unsafe._tool_by_name["agent"]._parent_agent is unsafe
