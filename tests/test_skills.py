"""Skills stay out of context until a model explicitly loads one."""

from types import SimpleNamespace

from kernelcoder import Agent, Config, cli
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


def test_skill_summary_respects_budget(tmp_path):
    _skill(tmp_path / "skills", "alpha", "A" * 500, "Body")
    _skill(tmp_path / "skills", "beta", "B" * 500, "Body")
    summary = SkillLoader([tmp_path / "skills"]).summary(char_budget=50)
    assert len(summary) <= 50
    assert "Body" not in summary


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
