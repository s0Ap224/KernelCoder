"""End-user startup contracts, isolated from real credentials and paid services."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from kernelcoder import cli, onboarding
from kernelcoder import config as cfg
from kernelcoder.demo import ScriptedLLM
from kernelcoder.sandbox import SandboxRunner

REAL_LOADER = cfg._load_dotenv


@pytest.fixture
def environment(tmp_path, monkeypatch):
    for aliases in cfg.ALIASES.values():
        for name in aliases:
            monkeypatch.delenv(name, raising=False)
    project = tmp_path / "project space 中文"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(cfg, "_load_dotenv", REAL_LOADER)
    cfg.user_dir().mkdir(parents=True)
    return project


def run(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["kernelcoder", *args])
    return cli.main()


def test_layered_aliases_and_shared_e2b_snapshot(environment, monkeypatch):
    (cfg.user_dir() / ".env").write_text("KERNELCODER_API_KEY=global\nE2B_API_KEY=global-e2b\n", encoding="utf-8")
    (environment / ".env").write_text("DEEPSEEK_API_KEY=project\nE2B_KEY=project-e2b\n", encoding="utf-8")
    explicit = environment / "chosen.env"
    explicit.write_text("OPENAI_API_KEY=explicit\nE2B_KEY=explicit-e2b\n", encoding="utf-8")
    with cfg.environment_scope(explicit):
        assert cfg.Config.from_env().api_key == "explicit"
        runner = SandboxRunner(sandbox_type=Mock())
        runner.check()
        assert runner._key == "explicit-e2b"
        explicit.write_text("OPENAI_API_KEY=changed\n", encoding="utf-8")
        assert cfg.Config.from_env().api_key == "explicit"
        assert "OPENAI_API_KEY" not in os.environ
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process")
    assert cfg.Config.from_env().api_key == "process"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert cfg.Config.from_env().api_key == "project"
    (environment / ".env").unlink()
    assert cfg.Config.from_env().api_key == "global"


def test_nearest_parent_and_literal_values(environment, monkeypatch):
    (environment / ".env").write_text("OPENAI_API_KEY='${UNRELATED}'\nKERNELCODER_MODEL=outer\n", encoding="utf-8")
    nested = environment / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert cfg.Config.from_env().api_key == "${UNRELATED}"
    (nested / ".env").write_text("KERNELCODER_MODEL=inner\n", encoding="utf-8")
    assert cfg.Config.from_env().model == "inner"
    assert cfg.Config.from_env().api_key == ""


def test_init_import_preserve_cancel_and_secret_roundtrip(environment, monkeypatch, capsys):
    source = environment / "import.env"
    secret = "secret'with\\slash$and=equals"
    onboarding.write_private(source, "OPENAI_API_KEY='imported'\nKERNELCODER_MODEL=imported-model\nOTHER_SECRET=ignore\n")
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda _: secret)
    run(monkeypatch, "init", "--mode", "local", "--from-env", str(source))
    values = cfg.read_env(cfg.user_dir() / ".env")
    assert values["KERNELCODER_API_KEY"] == secret
    assert values["KERNELCODER_MODEL"] == "imported-model"
    assert "OTHER_SECRET" not in values
    assert cfg.load_settings()["mode"] == "local"
    assert secret not in capsys.readouterr().out
    original = (cfg.user_dir() / ".env").read_bytes()
    answers = iter(["", "", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda _: "replacement")
    run(monkeypatch, "init", "--mode", "local")
    assert (cfg.user_dir() / ".env").read_bytes() == original
    answers = iter(["", "", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda _: "")
    run(monkeypatch, "init", "--mode", "local")
    assert (cfg.user_dir() / ".env").read_bytes() == original


def test_init_missing_key_and_missing_import_do_not_write(environment, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda _: "")
    with pytest.raises(SystemExit) as error:
        run(monkeypatch, "init", "--mode", "local")
    assert error.value.code == 1
    assert not (cfg.user_dir() / ".env").exists()
    with pytest.raises(SystemExit):
        run(monkeypatch, "init", "--from-env", str(environment / "missing"))


def test_local_global_default_runs_from_project_without_e2b(environment, monkeypatch):
    onboarding.write_private(cfg.user_dir() / ".env", "OPENAI_API_KEY=global\nKERNELCODER_MODEL=test\n")
    onboarding.save_settings({"mode": "local", "mcp": {"backend": "local", "python": "unused"}})
    monkeypatch.setattr(cli, "LLM", lambda **kwargs: ScriptedLLM([]))
    monkeypatch.setattr(cli, "load_mcp_tools", list)
    monkeypatch.setattr(cli, "load_hooks", lambda: None)
    monkeypatch.setattr(SandboxRunner, "check", Mock(side_effect=AssertionError("E2B called")))
    captured = []
    monkeypatch.setattr(cli, "_repl", lambda agent, config: captured.append((agent, config)))
    run(monkeypatch)
    agent, config = captured[0]
    assert agent.workspace is None
    assert config.api_key == "global"
    assert Path.cwd() == environment
    assert "local-smoke" in agent._tool_by_name["bash"].execute("printf local-smoke")
    with pytest.raises(SystemExit):
        run(monkeypatch, "--mcp-backend", "local")


def test_mode_alias_conflicts_and_cli_config_override(environment, monkeypatch):
    with pytest.raises(SystemExit) as error:
        run(monkeypatch, "--mode", "local", "--sandbox", "e2b")
    assert error.value.code == 2
    monkeypatch.setattr("sys.argv", ["kernelcoder"])
    assert cli._parse_args().sandbox == "e2b"
    monkeypatch.setattr(cli, "LLM", lambda **kwargs: ScriptedLLM([]))
    monkeypatch.setattr(cli, "load_mcp_tools", list)
    captured = []
    monkeypatch.setattr(cli, "_run_once", lambda agent, prompt: captured.append(agent.llm))
    run(monkeypatch, "--mode", "local", "--api-key", "cli-key", "--model", "cli-model", "-p", "hello")
    assert captured  # No configured key was necessary because the CLI supplied one.


def test_doctor_offline_redacted_and_missing_config(environment, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LLM", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(SandboxRunner, "probe", Mock(side_effect=AssertionError("E2B called")))
    secret = "not-for-output"
    explicit = environment / "chosen.env"
    explicit.write_text(f"OPENAI_API_KEY={secret}\nE2B_KEY={secret}\n", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        run(monkeypatch, "doctor", "--env-file", str(explicit))
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert secret not in output and "chosen.env" in output and "Offline" in output
    with pytest.raises(SystemExit) as error:
        run(monkeypatch, "doctor")
    assert error.value.code == 1
    explicit.write_text(f"KERNELCODER_MAX_TOKENS={secret}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        run(monkeypatch, "doctor", "--env-file", str(explicit))
    assert secret not in capsys.readouterr().out


def test_resume_e2b_overrides_local_default(environment, monkeypatch):
    onboarding.save_settings({"mode": "local"})
    monkeypatch.setattr(cli, "session_sandbox_id", lambda _: "saved-task")
    seen = []
    monkeypatch.setattr(onboarding, "doctor", lambda console, args, settings: seen.append(args.sandbox) or 0)
    with pytest.raises(SystemExit):
        run(monkeypatch, "doctor", "--resume", "saved-conversation")
    assert seen == ["e2b"]


def test_mcp_install_failure_preserves_settings_then_success(environment, monkeypatch):
    onboarding.save_settings({"mode": "local", "mcp": {"backend": "off"}})
    before = (cfg.user_dir() / "settings.json").read_bytes()
    monkeypatch.setattr(onboarding, "mcp_valid", lambda _: False)
    monkeypatch.setattr(onboarding.venv.EnvBuilder, "create", lambda *args: None)
    pip = Mock(return_value=Mock(returncode=1))
    monkeypatch.setattr(onboarding.subprocess, "run", pip)
    with pytest.raises(SystemExit):
        run(monkeypatch, "setup-mcp")
    assert (cfg.user_dir() / "settings.json").read_bytes() == before
    validity = iter([False, True])
    monkeypatch.setattr(onboarding, "mcp_valid", lambda _: next(validity))
    pip.return_value.returncode = 0
    run(monkeypatch, "setup-mcp")
    assert cfg.load_settings()["mcp"]["backend"] == "local"
    assert cfg.load_settings()["mode"] == "local"
    pip.reset_mock()
    monkeypatch.setattr(onboarding, "mcp_valid", lambda _: True)
    run(monkeypatch, "setup-mcp")
    pip.assert_not_called()


def test_invalid_settings_error_does_not_echo_content(environment, monkeypatch, capsys):
    (cfg.user_dir() / "settings.json").write_text('{"mode":"secret-value"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        run(monkeypatch, "doctor")
    assert "secret-value" not in capsys.readouterr().out
