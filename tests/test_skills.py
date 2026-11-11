"""Skills stay out of context until a model explicitly loads one."""

from types import SimpleNamespace

import pytest

from kernelcoder import Agent, Config, cli
from kernelcoder.context import CompactResult, ContextLimitError, ContextManager
from kernelcoder.demo import ScriptedLLM
from kernelcoder.llm import LLMResponse, ToolCall
from kernelcoder.permissions import Permission
from kernelcoder.skills import SkillLoader, SkillTool


def _skill(root, name, description, body):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def test_skill_body_is_loaded_only_after_tool_call(tmp_path):
    _skill(tmp_path / "skills", "review", "Review changes", "Check error paths.")
    loader = SkillLoader([tmp_path / "skills"])
    tool = SkillTool(loader)
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="s1", name="load_skill", arguments={"skill": "review"})]),
            LLMResponse(content="reviewed"),
        ]),
        tools=[tool],
        permission=Permission(),
    )

    system = agent._full_messages()[0]["content"]
    assert "review: Review changes" in system
    assert "Check error paths." not in system
    assert agent.chat("Review this change") == "reviewed"
    assert "Check error paths." in agent.messages[2]["content"]


def test_project_skill_overrides_user_skill_and_loads_latest_body(tmp_path):
    project = tmp_path / "project"
    user = tmp_path / "user"
    _skill(project, "review", "Project review", "Project instructions")
    _skill(user, "review", "User review", "User instructions")
    loader = SkillLoader([project, user])

    assert list(loader.skills) == ["review"]
    assert "Project review" in loader.summary()
    assert "User review" not in loader.summary()
    path = project / "review" / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nNew instruction.\n", encoding="utf-8")
    assert "New instruction." in loader.load("review")


def test_unknown_skill_cannot_read_arbitrary_path(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private content", encoding="utf-8")
    loader = SkillLoader([tmp_path / "skills"])

    result = loader.load(str(secret))
    assert "unknown skill" in result
    assert "private content" not in result


def test_full_catalog_is_not_truncated_or_duplicated_in_schema(tmp_path):
    _skill(tmp_path / "skills", "alpha", "A" * 500, "Body")
    _skill(tmp_path / "skills", "beta", "B" * 4000, "Body")
    loader = SkillLoader([tmp_path / "skills"])
    summary = loader.summary()
    assert "A" * 500 in summary
    assert "B" * 4000 in summary
    assert "Body" not in summary
    agent = Agent(ScriptedLLM([]), tools=[SkillTool(loader)])
    assert agent._full_messages()[0]["content"].count(summary) == 1
    assert "B" * 4000 not in str(agent._tool_schemas())


def test_skill_uses_heading_when_description_is_missing(tmp_path):
    path = tmp_path / "skills" / "plain" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Plain skill\n\nInstructions.\n", encoding="utf-8")
    assert "plain: Plain skill" in SkillLoader([tmp_path / "skills"]).summary()


def test_cli_registers_project_skills(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _skill(tmp_path / ".kernelcoder" / "skills", "review", "Review changes", "Instructions")
    monkeypatch.setattr(cli, "_parse_args", lambda: SimpleNamespace(
        demo=False, model=None, base_url=None, api_key=None, yes=False,
        prompt="review this", resume=None,
        sandbox="unsafe", sandbox_check=False, sandbox_action=None, sandbox_session=None,
        sandbox_image=None, sandbox_store=None, sandbox_exclude=[],
        mcp_backend=None, mcp_python=None,
    ))
    monkeypatch.setattr(cli.Config, "from_env", lambda: Config(model="test", api_key="test"))
    monkeypatch.setattr(cli, "LLM", lambda **kwargs: ScriptedLLM([]))
    monkeypatch.setattr(cli, "load_mcp_tools", list)
    monkeypatch.setattr(cli, "load_hooks", lambda: None)
    captured = []
    monkeypatch.setattr(cli, "_run_once", lambda agent, prompt: captured.append(agent))

    cli.main()

    assert "load_skill" in captured[0]._tool_by_name
    assert "review: Review changes" in captured[0]._full_messages()[0]["content"]


def test_multiline_yaml_and_diagnostics(tmp_path):
    root = tmp_path / "skills"
    _skill(root, "review", "|\n  Review changes.\n  Use for code review: include tests.", "Instructions")
    _skill(root, "bad", "[unclosed", "Invalid YAML")
    _skill(root, "empty", "42", "Invalid description type")
    loader = SkillLoader([root])
    assert list(loader.skills) == ["review"]
    assert loader.skills["review"].description == "Review changes. Use for code review: include tests."
    assert len(loader.diagnostics) == 2


def test_declared_name_and_collision_are_reported(tmp_path):
    project, user = tmp_path / "project", tmp_path / "user"
    _skill(project, "review", "Project", "Local")
    _skill(user, "review", "User", "Global")
    path = project / "review" / "SKILL.md"
    path.write_text(path.read_text().replace("name: review", "name: inspect"), encoding="utf-8")
    _skill(user, "inspect", "Duplicate", "Shadowed")
    loader = SkillLoader([project, user])
    assert "Local" in loader.load("inspect")
    assert any("differs from directory" in item for item in loader.diagnostics)
    assert any("shadowed" in item for item in loader.diagnostics)


def test_reload_empty_add_edit_remove(tmp_path):
    root = tmp_path / "skills"
    agent = Agent(ScriptedLLM([]), tools=[], skill_loader=SkillLoader([root]))
    assert "load_skill" not in agent._tool_by_name
    assert "# Available skills" not in agent._full_messages()[0]["content"]
    assert "Skills: 0" in agent.describe_skills()
    _skill(root, "review", "Original", "First")
    agent.reload_skills()
    assert "load_skill" in agent._tool_by_name
    assert "review: Original" in agent._full_messages()[0]["content"]
    path = root / "review" / "SKILL.md"
    path.write_text(path.read_text().replace("Original", "Updated"), encoding="utf-8")
    assert "metadata changed" in agent.skill_loader.load("review")
    agent.reload_skills()
    assert "review: Updated" in agent._full_messages()[0]["content"]
    path.unlink()
    agent.reload_skills()
    assert "load_skill" not in agent._tool_by_name
    assert "load_skill" not in str(agent._tool_schemas())
    assert "# Available skills" not in agent._full_messages()[0]["content"]


class RecordingLLM(ScriptedLLM):
    def __init__(self, script):
        super().__init__(script)
        self.calls = []

    def chat(self, **kwargs):
        import copy
        self.calls.append(copy.deepcopy(kwargs))
        return super().chat(**kwargs)


def test_explicit_skill_loads_before_first_request_and_keeps_pairing(tmp_path):
    _skill(tmp_path, "review", "Review code", "UNIQUE INSTRUCTION")
    llm = RecordingLLM([LLMResponse(content="done")])
    agent = Agent(llm, tools=[], skill_loader=SkillLoader([tmp_path]))
    assert agent.chat("/skill review inspect app.py") == "done"
    sent = llm.calls[0]["messages"]
    assert sent[1]["content"] == "/skill review inspect app.py"
    assert "UNIQUE INSTRUCTION" in sent[3]["content"]
    agent.context.units(agent.messages)
    assert agent.context._meta(agent.messages[2])["skill_active"]


def test_explicit_unknown_or_denied_skill_never_calls_model(tmp_path):
    from kernelcoder.permissions import PermissionRule

    _skill(tmp_path, "review", "Review code", "UNIQUE INSTRUCTION")
    agent = Agent(ScriptedLLM([]), tools=[], skill_loader=SkillLoader([tmp_path]),
                  permission=Permission(rules=[PermissionRule("deny", "load_skill")]))
    assert "unknown skill" in agent.chat("/skill missing")
    assert not agent.messages
    assert "Usage:" in agent.chat("/skill")
    result = agent.chat("/skill review")
    assert "UNIQUE INSTRUCTION" not in result
    assert not any(agent.context._meta(m).get("skill_active") for m in agent.messages)
    agent.context.units(agent.messages)


def test_same_version_reused_but_changed_body_is_loaded_again(tmp_path):
    _skill(tmp_path, "review", "Review", "FULL ORIGINAL BODY")
    llm = ScriptedLLM([LLMResponse(content="done") for _ in range(3)])
    agent = Agent(llm, tools=[], skill_loader=SkillLoader([tmp_path]))
    agent.chat("/skill review first task")
    original = agent.messages[2]
    agent.chat("/skill review second task")
    assert "reused" in agent.messages[-2]["content"]
    assert agent.context._meta(original)["skill_active"]
    path = tmp_path / "review" / "SKILL.md"
    path.write_text(path.read_text() + "\nNEW VERSION", encoding="utf-8")
    agent.chat("/skill review third task")
    assert "NEW VERSION" in agent.messages[-2]["content"]
    assert not agent.context._meta(original).get("skill_active")


def test_active_skill_survives_all_compaction_stages_and_session_restore(tmp_path, monkeypatch):
    _skill(tmp_path, "review", "Review", "COMPLETE BODY " * 9000)
    agent = Agent(ScriptedLLM([LLMResponse(content="done")]), tools=[], skill_loader=SkillLoader([tmp_path]))
    agent.chat("/skill review")
    skill_message = agent.messages[2]
    original = skill_message["content"]
    assert len(original) > 100000  # even the ingestion budget must not truncate active instructions
    for index in range(45):
        agent._append_message({"role": "assistant", "content": f"progress {index}"})
    assert skill_message in agent.messages
    agent.context.units(agent.messages)
    agent.context.micro_compact(agent.messages, 0, CompactResult(0, 0))
    summary = RecordingLLM([LLMResponse(content="Progress summary")])
    agent.context.compact_history(agent.messages, summary, 0, CompactResult(0, 0))
    assert skill_message["content"] == original
    assert "COMPLETE BODY" not in summary.calls[0]["messages"][-1]["content"]
    from kernelcoder import session
    from kernelcoder.session import load_context_state, load_session, save_session
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path / "sessions")
    sid = save_session(agent.messages, "test", context_state=agent.context.export_state(agent.messages))
    messages, _ = load_session(sid)
    restored = ContextManager()
    restored.restore_state(messages, load_context_state(sid))
    assert any(restored._meta(m).get("skill_active") for m in messages)
    restored.compact_history(messages, None, 0, CompactResult(0, 0))
    assert any(m.get("content") == original for m in messages)
    restored.units(messages)


def test_trimmed_old_skill_is_reloaded_and_reset_clears_activation(tmp_path):
    _skill(tmp_path, "review", "Review", "INSTRUCTIONS " * 500)
    agent = Agent(ScriptedLLM([LLMResponse(content="done") for _ in range(3)]),
                  tools=[], skill_loader=SkillLoader([tmp_path]))
    agent.chat("/skill review")
    old = agent.messages[2]
    agent.chat("unrelated question")
    assert not agent.context._meta(old).get("skill_active")
    agent.context._preview(old, 150, "micro_compact", CompactResult(0, 0))
    assert "Saved tool result" in old["content"]
    agent.chat("/skill review continue")
    assert "INSTRUCTIONS " * 500 in agent.messages[-2]["content"]
    agent.reset()
    assert not agent.messages
    assert not agent.context._records


def test_full_catalog_or_skill_overflow_is_explicit(tmp_path):
    _skill(tmp_path, "review", "Review", "X" * 20000)
    agent = Agent(ScriptedLLM([]), tools=[], skill_loader=SkillLoader([tmp_path]), max_context_tokens=5000)
    with pytest.raises(ContextLimitError, match="active skills"):
        agent.chat("/skill review")
    assert "X" * 20000 in agent.messages[-1]["content"]
    path = tmp_path / "review" / "SKILL.md"
    path.write_text("---\nname: review\ndescription: " + "Z" * 20000 + "\n---\nBody", encoding="utf-8")
    agent.reset()
    agent.reload_skills()
    with pytest.raises(ContextLimitError):
        agent.chat("hello")
    assert "Z" * 20000 in agent._full_messages()[0]["content"]


def test_repl_lists_reloads_and_accepts_explicit_skill(monkeypatch, tmp_path, capsys):
    from .conftest import repl_with

    _skill(tmp_path, "review", "Review code", "Instructions")
    agent = Agent(ScriptedLLM([LLMResponse(content="done")]), tools=[], skill_loader=SkillLoader([tmp_path]))
    repl_with(monkeypatch, agent, ["/skills", "/skills reload", "/skill review inspect", "quit"])
    output = capsys.readouterr().out
    assert "Skills: 1" in output
    assert "Unknown command" not in output
    assert any(agent.context._meta(m).get("skill_active") for m in agent.messages)


def test_parallel_skill_results_keep_complete_batches(tmp_path):
    _skill(tmp_path, "review", "Review", "REVIEW BODY " * 10000)
    _skill(tmp_path, "test", "Test", "TEST BODY")
    calls = [ToolCall(id="r", name="load_skill", arguments={"skill": "review"}),
             ToolCall(id="t", name="load_skill", arguments={"skill": "test"})]
    llm = ScriptedLLM([LLMResponse(tool_calls=calls), LLMResponse(content="done")])
    agent = Agent(llm, tools=[], skill_loader=SkillLoader([tmp_path]))
    agent.chat("review and test")
    assert sum(bool(agent.context._meta(m).get("skill_active")) for m in agent.messages) == 2
    assert any("REVIEW BODY " * 10000 in (m.get("content") or "") for m in agent.messages)
    agent.context.units(agent.messages)


def test_skill_read_rechecks_path_and_does_not_read_resources(tmp_path, monkeypatch):
    from pathlib import Path

    _skill(tmp_path, "review", "Review", "Read references/later.md only when needed.")
    resource = tmp_path / "review" / "references" / "later.md"
    resource.parent.mkdir()
    resource.write_text("RESOURCE BODY", encoding="utf-8")
    loader = SkillLoader([tmp_path])
    assert "RESOURCE BODY" not in loader.load("review")
    original = Path.is_symlink
    manifest = tmp_path / "review" / "SKILL.md"
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == manifest or original(path))
    assert "cannot read skill" in loader.load("review")


def test_child_agent_activation_does_not_change_parent(tmp_path):
    from kernelcoder.tools.agent import AgentTool

    _skill(tmp_path, "review", "Review", "BODY")
    agent = Agent(ScriptedLLM([LLMResponse(content="child done")]), tools=[AgentTool()],
                  skill_loader=SkillLoader([tmp_path]))
    result = agent._tool_by_name["agent"].execute("/skill review inspect")
    assert "child done" in result
    assert not agent.messages
    assert not agent.context._records
