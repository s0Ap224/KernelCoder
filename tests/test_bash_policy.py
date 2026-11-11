"""Permission decisions must prevent execution through every approval path."""
import json

import pytest

from kernelcoder import Agent
from kernelcoder.bash_policy import analyze_command
from kernelcoder.demo import ScriptedLLM
from kernelcoder.llm import ToolCall
from kernelcoder.permissions import Permission, PermissionDecision, PermissionRule
from kernelcoder.tools.bash import BashTool
from kernelcoder.tools.read import ReadFileTool
from kernelcoder.tools.write import WriteFileTool


def rule(behavior, content=None, match="exact"):
    return PermissionRule(behavior, "bash", content, match)


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypass"])
def test_restrictions_precede_modes(mode):
    p = Permission(mode=mode, rules=[rule("allow"), rule("ask", "npm publish", "prefix"), rule("deny", "git push")])
    assert p.evaluate(BashTool(), {"command": "git push"}).behavior == "deny"
    assert p.evaluate(BashTool(), {"command": "npm publish --tag beta"}).behavior == "ask"
    assert p.evaluate(BashTool(), {"command": "rm -rf /"}).behavior == "deny"


@pytest.mark.parametrize("command", ["git status; npm publish", "git status > file", "git status $(id)",
                                      "git status && bash -c 'id'", "git status || npm publish"])
def test_prefix_does_not_authorize_other_effects(command):
    p = Permission(rules=[rule("allow", "git status", "prefix")])
    assert p.evaluate(BashTool(), {"command": command}).behavior == "ask"


def test_all_components_need_grants():
    p = Permission(rules=[rule("allow", "git status"), rule("allow", "git diff")])
    assert p.evaluate(BashTool(), {"command": "git status && git diff"}).behavior == "allow"
    assert p.evaluate(BashTool(), {"command": "git statusx"}).behavior == "ask"


@pytest.mark.parametrize("command", ["echo 'a;b'", "echo 中文", "git status --short"])
def test_simple_commands_reusable(command):
    assert analyze_command(command).reusable


def test_forced_ask_cannot_be_saved(tmp_path):
    p = Permission(ask=lambda *_: "project", persist_path=tmp_path / "p.json", rules=[rule("ask")])
    assert p.authorize(BashTool(), {"command": "git status"}) is not None
    assert not (tmp_path / "p.json").exists()


def test_project_scope_and_legacy(tmp_path):
    store = tmp_path / "permissions.json"
    p = Permission(ask=lambda *_: "project", project_root=tmp_path / "a", persist_path=store)
    assert p.authorize(BashTool(), {"command": "git status"}) is None
    assert Permission(project_root=tmp_path / "a", persist_path=store).check("bash", {"command": "git status"}) is None
    assert Permission(project_root=tmp_path / "b", persist_path=store).check("bash", {"command": "git status"})
    store.write_text(json.dumps({"always": ["bash"]}))
    with pytest.warns(UserWarning, match="Legacy"):
        p = Permission(persist_path=store)
    assert p.check("bash", {"command": "git status"})


def test_custom_read_and_default_agent():
    class Impostor(ReadFileTool):
        pass
    assert Permission().evaluate(Impostor(), {}).behavior == "ask"
    agent = Agent(ScriptedLLM([]), tools=[WriteFileTool()])
    assert agent._permit(ToolCall("x", "write_file", {"file_path": "a", "content": "x"}))


def test_prepared_command_is_what_runs(tmp_path, monkeypatch):
    tool = BashTool(unsafe=True)
    seen = []
    agent = Agent(ScriptedLLM([]), tools=[tool], permission=Permission(ask=lambda n, a: seen.append(dict(a)) or "once"))
    call = ToolCall("x", "bash", {"command": "echo hello", "cwd": str(tmp_path)})
    assert agent._permit(call) is None
    monkeypatch.setattr(tool, "execute_prepared", lambda **kwargs: kwargs)
    result = agent._exec_tool(call)
    assert result["command"] == seen[0]["command"]
    assert result["cwd"] == seen[0]["cwd"]
    assert result["prepared"].command == seen[0]["command"]


def test_accept_edits_and_sensitive_paths(tmp_path):
    p = Permission(mode="acceptEdits", project_root=tmp_path)
    assert p.evaluate(WriteFileTool(), {"file_path": str(tmp_path / "a.py")}).behavior == "allow"
    assert p.evaluate(WriteFileTool(), {"file_path": str(tmp_path / ".env")}).behavior == "ask"


def test_cli_displays_full_command(monkeypatch, capsys):
    from kernelcoder import cli
    monkeypatch.setattr(cli, "pt_prompt", lambda *_: "y")
    command = "echo " + "x" * 200 + " END_MARKER"
    assert cli._ask_permission("bash", {"command": command}, decision=PermissionDecision("ask", "reason", "default")) == "once"
    assert "END_MARKER" in capsys.readouterr().out


def test_cmd_never_uses_posix_allow():
    p = Permission(mode="bypass", rules=[rule("allow")])
    assert p.evaluate(BashTool(), {"command": "echo ok"}, context={"dialect": "cmd"}).behavior == "ask"


def test_saved_grant_binds_directory_and_backend(tmp_path):
    store = tmp_path / "permissions.json"
    context = {"cwd": str(tmp_path), "execution": "local"}
    p = Permission(ask=lambda *_: "project", persist_path=store)
    assert p.authorize(BashTool(), {"command": "git status"}, context=context) is None
    loaded = Permission(persist_path=store)
    assert loaded.evaluate(BashTool(), {"command": "git status"}, context=context).behavior == "allow"
    for changed in ({**context, "cwd": str(tmp_path / "other")}, {**context, "execution": "e2b"}):
        assert loaded.evaluate(BashTool(), {"command": "git status"}, context=changed).behavior == "ask"


def test_concurrent_store_change_is_not_overwritten(tmp_path):
    store = tmp_path / "permissions.json"
    first = Permission(ask=lambda *_: "project", persist_path=store)
    second = Permission(ask=lambda *_: "project", persist_path=store)
    assert first.check("bash", {"command": "git status"}) is None
    saved = store.read_bytes()
    with pytest.warns(UserWarning, match="save failed"):
        assert second.check("bash", {"command": "git diff"}) is None
    assert store.read_bytes() == saved
    assert second.check("bash", {"command": "git diff"}) is None


def test_prepare_failure_and_interrupt_never_execute(tmp_path, monkeypatch):
    called = []
    tool = BashTool(unsafe=True)
    p = Permission(ask=lambda *_: called.append("asked") or "once")
    agent = Agent(ScriptedLLM([]), tools=[tool], permission=p)
    assert agent._permit(ToolCall("x", "bash", {"command": "echo ok", "timeout": True}))
    assert not called
    def interrupt(*args):
        raise KeyboardInterrupt
    p.ask = interrupt
    assert "interrupted" in agent._permit(ToolCall("y", "bash", {"command": "echo ok", "cwd": str(tmp_path)}))
