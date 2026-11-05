"""Shared pytest fixtures and helpers."""

import pytest

from kernelcoder import Config, checkpoints, cli, onboarding
from kernelcoder import config as config_module
from kernelcoder import llm as llm_module
from kernelcoder.tools import ALL_TOOLS


def pytest_addoption(parser):
    parser.addoption('--run-e2b', action='store_true', default=False, help='Run billable E2B acceptance tests')


@pytest.fixture(autouse=True)
def _checkpoints_isolated(tmp_path, monkeypatch):
    """Checkpoint persistence must never touch the real ~/.kernelcoder."""
    monkeypatch.setattr(checkpoints, "CHECKPOINTS_FILE", tmp_path / "checkpoints.json")
    checkpoints.clear()
    yield
    checkpoints.clear()


@pytest.fixture(autouse=True)
def _pricing_isolated(tmp_path, monkeypatch):
    """Cost estimates must never read the real ~/.kernelcoder/pricing.json."""
    monkeypatch.setattr(llm_module, "PRICING_FILE", tmp_path / "pricing.json")


@pytest.fixture(autouse=True)
def _dotenv_isolated(monkeypatch):
    """Developer credentials and model choices must not affect test defaults."""
    monkeypatch.setattr("kernelcoder.config._load_dotenv", lambda: None)
    monkeypatch.delenv('E2B_API_KEY', raising=False)
    monkeypatch.delenv('E2B_KEY', raising=False)


@pytest.fixture(autouse=True)
def _user_config_isolated(tmp_path, monkeypatch):
    """New global configuration/history must never read or write the real user's files."""
    directory = tmp_path / "user" / ".kernelcoder"
    for module in (config_module, cli, onboarding):
        monkeypatch.setattr(module, "user_dir", lambda: directory)


def get_tool(name: str):
    """Look up a tool by name."""
    if name == "bash":
        # Legacy shell tests explicitly exercise the opt-in host backend.
        from kernelcoder.tools.bash import BashTool
        return BashTool(unsafe=True)
    for t in ALL_TOOLS:
        if t.name == name:
            return t
    return None


def repl_with(monkeypatch, agent, inputs, config=None):
    """Drive the real REPL with a scripted list of inputs. Returns the config in use."""
    it = iter(inputs)
    monkeypatch.setattr(cli, "pt_prompt", lambda *a, **k: next(it))
    config = config or Config.from_env()
    cli._repl(agent, config)
    return config
