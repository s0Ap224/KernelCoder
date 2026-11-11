"""Managed local jobs: policy, lifecycle, lazy CLI and opt-in real servers."""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock

import pytest

from kernelcoder.agent import Agent
from kernelcoder.demo import ScriptedLLM
from kernelcoder.llm import LLMResponse, ToolCall
from kernelcoder.mcp import MCPClient, MCPError
from kernelcoder.mcp_local import MCPManager, clean_environment, revision
from kernelcoder.permissions import Permission
from kernelcoder.workspace import SandboxWorkspace, WorkspaceError
from tests.test_mcp import FAKE_SERVER


@pytest.fixture
def task(tmp_path):
    original = tmp_path / "project 中文"
    original.mkdir()
    (original / "a.py").write_text("x = 1\n", encoding="utf-8")
    (original / ".env").write_text("SECRET=keep")
    return SandboxWorkspace.create(original, tmp_path / "tasks")


@pytest.fixture
def manager(task):
    return MCPManager(task)


def test_registration_is_lazy_without_e2b_or_subprocess(task, monkeypatch):
    monkeypatch.setattr("kernelcoder.prompt.platform.uname", lambda: SimpleNamespace(system="test", release="0", machine="test"))
    monkeypatch.setattr("subprocess.Popen", Mock(side_effect=AssertionError("process started")))
    monkeypatch.setattr("kernelcoder.sandbox.SandboxRunner.check", Mock(side_effect=AssertionError("E2B reached")))
    manager = MCPManager(task)
    assert len(manager.tools) == 5
    assert "not started" in manager.status()
    Agent(ScriptedLLM([]), tools=manager.tools, workspace=task)


def test_managed_tool_cannot_attach_to_another_workspace(manager, tmp_path):
    with pytest.raises(ValueError, match="bound"):
        Agent(ScriptedLLM([]), tools=manager.tools)
    other = SandboxWorkspace.create(manager.workspace.original, tmp_path / "other")
    with pytest.raises(ValueError, match="bound"):
        Agent(ScriptedLLM([]), tools=manager.tools, workspace=other)


def test_old_name_grant_does_not_authorize_managed_host_execution(manager):
    tool = manager.tools[0]
    permission = Permission()
    permission._always.add(tool.name)
    agent = Agent(ScriptedLLM([]), tools=manager.tools, workspace=manager.workspace, permission=permission)
    call = ToolCall("id", tool.name, {})
    assert "Permission denied" in agent._permit(call)
    permission._always.add(tool.permission_key)
    assert agent._permit(call) is None
    agent.plan_mode = True
    assert agent._permit(call) is None


def test_tool_rejects_repository_and_path_overrides(manager):
    with pytest.raises(ValueError, match="overrides"):
        manager.tools[0].execute(repo_path="C:/")
    with pytest.raises(ValueError, match="nonempty"):
        manager.tools[-1].execute(paths=[])


def test_environment_does_not_inherit_secrets(tmp_path, monkeypatch):
    for name in ("E2B_API_KEY", "OPENAI_API_KEY", "PYTHONPATH", "GIT_CONFIG_COUNT", "SEMGREP_APP_TOKEN"):
        monkeypatch.setenv(name, "secret")
    env = clean_environment(tmp_path, sys.executable)
    assert not any("secret" == value for value in env.values())
    assert env["HOME"] == str(tmp_path)
    assert env["SEMGREP_SEND_METRICS"] == "off"


def test_job_error_is_recorded_and_has_no_original_side_effects(manager, monkeypatch):
    monkeypatch.setattr(manager.runner, "_versions", Mock(side_effect=MCPError("missing pinned environment")))
    before = manager.workspace.scan()
    result = manager.runner.run("git", "git_status", {})
    assert result.status == "error"
    record = manager.workspace.directory / "mcp-jobs" / result.job.id / "result.json"
    saved = json.loads(record.read_text())
    assert saved["job"]["revision_id"] == revision(before)
    assert saved["output_revision_id"] == revision(before)
    assert manager.workspace.scan() == before
    assert (manager.workspace.original / "a.py").read_text() == "x = 1\n"


def test_mismatched_runtime_is_refused(manager, monkeypatch):
    monkeypatch.setattr("kernelcoder.mcp_local.subprocess.run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps({"packages": {"semgrep": "other"}})))
    with pytest.raises(MCPError, match="version mismatch"):
        manager.runner._versions({})


def test_dirty_workspace_blocks_mcp(manager):
    ws = manager.workspace
    ws.meta["active"] = {"backend": "e2b", "phase": "syncing"}
    ws._save()
    with pytest.raises(WorkspaceError, match="Unclean"):
        manager.tools[0].execute()


def test_cli_local_is_lazy_and_saved_without_e2b(task, monkeypatch, capsys):
    from kernelcoder import cli
    monkeypatch.setattr(cli.SandboxRunner, "check", Mock(side_effect=AssertionError("E2B reached")))
    monkeypatch.setattr(cli, "load_mcp_tools", Mock(side_effect=AssertionError("legacy MCP started")))
    monkeypatch.setattr("kernelcoder.session.SESSIONS_DIR", task.directory.parent / "chats")
    monkeypatch.setattr(cli.Config, "from_env", lambda: SimpleNamespace(
        model="test", api_key="test", base_url=None, provider="openai", temperature=0,
        max_tokens=100, max_context_tokens=128_000))
    monkeypatch.setattr(cli, "LLM", lambda **kw: ScriptedLLM([LLMResponse(content="ready")]))
    monkeypatch.setattr("sys.argv", ["kernelcoder", "--sandbox-session", task.id,
                                   "--sandbox-store", str(task.directory.parent),
                                   "--mcp-backend", "local", "--mcp-python", "relative/python.exe", "-p", "hello"])
    cli.main()
    reopened = SandboxWorkspace.open(task.id, task.directory.parent)
    assert reopened.meta["mcp"]["backend"] == "local"
    saved_python = reopened.meta["mcp"]["python"]
    assert os.path.isabs(saved_python)
    assert "remote_id" not in reopened.meta
    assert "not started" in capsys.readouterr().out
    monkeypatch.chdir(task.original)
    monkeypatch.setattr("sys.argv", ["kernelcoder", "--sandbox-session", task.id,
                                   "--sandbox-store", str(task.directory.parent), "-p", "resume"])
    cli.main()
    reopened = SandboxWorkspace.open(task.id, task.directory.parent)
    assert reopened.meta["mcp"] == {"backend": "local", "python": saved_python}


def test_managed_process_transport_and_timeout_cleanup(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    client = MCPClient("fake", sys.executable, [str(script)],
                       clean_environment(tmp_path, sys.executable), cwd=tmp_path, managed=True)
    try:
        assert client.call_result("echo", {"text": "中文"})["content"][0]["text"] == "echo: 中文"
        client.call_timeout = 0.1
        with pytest.raises(MCPError, match="no answer"):
            client.call_result("stall", {})
        assert not client._pending
    finally:
        client.close()
    assert client._proc.poll() is not None


def test_managed_close_stops_descendants(tmp_path):
    marker = tmp_path / "escaped.txt"
    child = f"import time,pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('escaped')"
    script = tmp_path / "server.py"
    script.write_text(
        "import subprocess,sys\nsubprocess.Popen([sys.executable, '-c', " + repr(child) + "])\n" + FAKE_SERVER,
        encoding="utf-8")
    client = MCPClient("fake", sys.executable, [str(script)],
                       clean_environment(tmp_path, sys.executable), cwd=tmp_path, managed=True)
    client.close()
    time.sleep(2.2)
    assert not marker.exists()


def test_pagination_and_unsupported_server_requests(tmp_path):
    script = tmp_path / "paged.py"
    script.write_text("""
import json, sys
for line in sys.stdin:
    r = json.loads(line)
    if "method" not in r or "id" not in r:
        continue
    if r["method"] == "initialize":
        result = {"protocolVersion":"2025-06-18", "capabilities":{"tools":{}}}
    elif r["method"] == "tools/list":
        if not r["params"]:
            print(json.dumps({"jsonrpc":"2.0", "id":"server-1", "method":"sampling/createMessage"}), flush=True)
            result = {"tools":[], "nextCursor":"page2"}
        else:
            result = {"tools":[{"name":"echo", "inputSchema":{"type":"object"}}]}
    else:
        result = {"content":[], "structuredContent":{"value":42}}
    print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":result}), flush=True)
""", encoding="utf-8")
    client = MCPClient("fake", sys.executable, [str(script)],
                       clean_environment(tmp_path, sys.executable), cwd=tmp_path, managed=True)
    try:
        assert len(client.tools) == 1
        assert client.call_result("echo", {})["structuredContent"] == {"value": 42}
        assert not client._responses
    finally:
        client.close()


def test_cancel_and_schema_mismatch_close_and_record(manager, monkeypatch):
    import kernelcoder.mcp_local as local
    monkeypatch.setattr(manager.runner, "_versions", lambda env: {})
    monkeypatch.setattr(manager.runner, "_git_view", lambda *args: None)
    class Client:
        stderr = ""
        closed = 0
        tool_specs: ClassVar[list] = [{"name": name, "inputSchema": schema}
                                     for name, schema in manager.profiles["git"].tools.items()]

        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            Client.closed += 1

        def call_result(self, *args):
            raise KeyboardInterrupt()
    monkeypatch.setattr(local, "MCPClient", Client)
    with pytest.raises(KeyboardInterrupt):
        manager.runner.run("git", "git_status", {})
    assert manager.runner.last_result.status == "cancelled"
    assert Client.closed
    Client.tool_specs = []
    result = manager.runner.run("git", "git_status", {})
    assert result.status == "error" and "schema mismatch" in result.detail


def test_outside_scan_path_is_refused_before_service_start(manager, monkeypatch):
    import kernelcoder.mcp_local as local
    monkeypatch.setattr(manager.runner, "_versions", lambda env: {})
    monkeypatch.setattr(local, "MCPClient", Mock(side_effect=AssertionError("service started")))
    result = manager.runner.run("semgrep", "semgrep_scan_with_custom_rule", {"paths": ["../secret.py"]})
    assert result.status == "error" and "not in task manifest" in result.detail
    with pytest.raises(ValueError, match="allowlist"):
        manager.runner.run("git", "git_commit", {})


def test_git_view_handles_file_directory_transitions(task, tmp_path):
    manager = MCPManager(task)
    (task.tree / "a.py").unlink()
    (task.tree / "a.py").mkdir()
    (task.tree / "a.py" / "new.py").write_text("nested = True\n")
    view = tmp_path / "git-view"
    view.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    manager.runner._git_view(view, task.scan(), clean_environment(home, sys.executable))
    assert (view / "a.py" / "new.py").read_text() == "nested = True\n"
    assert (task.original / "a.py").read_text() == "x = 1\n"


def test_managed_scan_accepts_os_temp_alias(manager, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from pathlib import Path

    import kernelcoder.mcp_local as local

    real = tmp_path / "real-temp"
    real.mkdir()
    alias = tmp_path / "temp-alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable")
    original_temporary_directory = local.tempfile.TemporaryDirectory

    @contextmanager
    def linked_temp(**kwargs):
        with original_temporary_directory(dir=real, **kwargs) as directory:
            yield str(alias / Path(directory).name)

    monkeypatch.setattr(local.tempfile, "TemporaryDirectory", linked_temp)
    monkeypatch.setattr(manager.runner, "_versions", lambda env: {})
    client = Mock(stderr="", tool_specs=[{"name": name, "inputSchema": schema}
                  for name, schema in manager.profiles["semgrep"].tools.items()])
    client.call_result.return_value = {"content": []}
    factory = Mock(return_value=client)
    monkeypatch.setattr(local, "MCPClient", factory)
    result = manager.runner.run("semgrep", "semgrep_scan_with_custom_rule", {"paths": ["a.py"]})
    assert result.status == "completed", result.detail
    assert factory.call_args.kwargs["cwd"].is_relative_to(real.resolve())
    original_content = (manager.workspace.tree / "a.py").read_bytes().decode("utf-8")
    assert client.call_result.call_args.args[1]["code_files"] == [{"path": "a.py", "content": original_content}]


def test_real_local_git_and_semgrep(task, monkeypatch):
    python = os.environ.get("KERNELCODER_TEST_MCP_PYTHON")
    if not python:
        pytest.skip("Set KERNELCODER_TEST_MCP_PYTHON to the pinned tools environment")
    monkeypatch.setattr("kernelcoder.sandbox.SandboxRunner.check", Mock(side_effect=AssertionError("E2B reached")))
    def git(*args):
        return subprocess.check_output(["git", "-C", str(task.original), *args])
    git("init", "--quiet")
    git("add", "a.py")
    git("-c", "user.name=Test", "-c", "user.email=test@localhost",
        "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "original")
    (task.original / "a.py").write_text("user staged change\n")
    git("add", "a.py")
    (task.original / "a.py").write_text("user unstaged change\n")
    (task.original / "untracked.txt").write_text("keep me")
    original_status = git("status", "--porcelain=v1")
    original_index = (task.original / ".git" / "index").read_bytes()
    manager = MCPManager(task, python)
    (task.tree / "a.py").write_text("x = 2\n", encoding="utf-8")
    for name in ("left", "right"):
        (task.tree / name).mkdir()
    (task.tree / "left" / "same.py").write_text("eval(user_input)\n", encoding="utf-8")
    (task.tree / "right" / "same.py").write_text("print('safe')\n", encoding="utf-8")
    before = task.scan()
    for tool in manager.tools[:4]:
        result = tool.execute()
        assert "[completed;" in result, result
    diff = manager.tools[1].execute()
    assert "+x = 2" in diff, diff
    result = manager.tools[-1].execute(paths=["left/same.py", "right/same.py"])
    assert "[completed;" in result, result
    raw = manager.runner.last_result.result
    content = raw.get("structuredContent") or json.loads(raw["content"][0]["text"])
    assert len(content["results"]) == 1, content
    assert content["results"][0]["path"].replace("\\", "/") == "left/same.py", content
    assert not content["errors"], content
    result = manager.tools[-1].execute(paths=["right/same.py"])
    assert "[completed;" in result, result
    raw = manager.runner.last_result.result
    content = raw.get("structuredContent") or json.loads(raw["content"][0]["text"])
    assert not content["results"] and not content["errors"], content
    assert task.scan() == before
    assert (task.original / "a.py").read_text() == "user unstaged change\n"
    assert (task.original / "untracked.txt").read_text() == "keep me"
    assert (task.original / ".git" / "index").read_bytes() == original_index
    assert git("status", "--porcelain=v1") == original_status
