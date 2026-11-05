"""Layered configuration without exporting credentials to child processes."""

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

ALIASES = {
    "KERNELCODER_API_KEY": ("KERNELCODER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"),
    "KERNELCODER_BASE_URL": ("OPENAI_BASE_URL", "KERNELCODER_BASE_URL"),
    "E2B_API_KEY": ("E2B_API_KEY", "E2B_KEY"),
    **{name: (name,) for name in (
        "KERNELCODER_MODEL", "KERNELCODER_MAX_TOKENS", "KERNELCODER_TEMPERATURE",
        "KERNELCODER_MAX_CONTEXT", "KERNELCODER_PROVIDER",
    )},
}
_ENV_FILE = ContextVar("kernelcoder_env_file", default=None)
_SNAPSHOT = ContextVar("kernelcoder_env_snapshot", default=None)
_SCOPED = ContextVar("kernelcoder_env_scoped", default=False)


def user_dir():
    return Path.home() / ".kernelcoder"


def canonical(values):
    """Resolve aliases within a layer before merging layers."""
    return {name: next(values[key] for key in aliases if values.get(key))
            for name, aliases in ALIASES.items() if any(values.get(key) for key in aliases)}


def read_env(path, *, required=False):
    path = Path(path).expanduser()
    if not path.is_file():
        if required:
            raise ValueError("Environment file does not exist or is not a regular file.")
        return {}
    return dict(dotenv_values(path, encoding="utf-8-sig", interpolate=False))


def project_env():
    cur = Path.cwd()
    for directory in (cur, *cur.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
        if directory == Path.home():
            break
    return None


@contextmanager
def environment_scope(env_file=None):
    tokens = (_ENV_FILE.set(env_file), _SNAPSHOT.set(None), _SCOPED.set(True))
    try:
        yield
    finally:
        _SCOPED.reset(tokens[2])
        _SNAPSHOT.reset(tokens[1])
        _ENV_FILE.reset(tokens[0])


def _load_dotenv():
    """Resolve once per CLI invocation, without mutating os.environ."""
    cached = _SNAPSHOT.get()
    if cached is not None:
        return cached
    values, sources = {}, {}
    global_path = user_dir() / ".env"
    layers = [(read_env(global_path), str(global_path))]
    project = project_env()
    if project:
        layers.append((read_env(project), str(project)))
    explicit = _ENV_FILE.get()
    if explicit is not None:
        layers.append((read_env(explicit, required=True), str(Path(explicit).expanduser().absolute())))
    layers.append((os.environ, "process environment"))
    for layer, source in layers:
        for name, value in canonical(layer).items():
            values[name], sources[name] = value, source
    result = (values, sources)
    if _SCOPED.get():
        _SNAPSHOT.set(result)
    return result


def resolved_environment():
    # Support embedders/tests that explicitly disable dotenv loading.
    return _load_dotenv() or (canonical(os.environ), {})


def load_settings():
    path = user_dir() / "settings.json"
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict) or settings.get("mode", "e2b") not in ("local", "e2b"):
            raise ValueError
        mcp = settings.get("mcp", {})
        if not isinstance(mcp, dict) or mcp.get("backend", "off") not in ("local", "off"):
            raise ValueError
        if "python" in mcp and not isinstance(mcp["python"], str):
            raise ValueError
        return settings
    except (ValueError, TypeError):
        raise ValueError("Invalid ~/.kernelcoder/settings.json; fix or remove this file.") from None


@dataclass
class Config:
    model: str = "gpt-5.5"
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = 128_000
    provider: str = "openai"

    @classmethod
    def from_env(cls) -> "Config":
        values, _ = resolved_environment()
        try:
            return cls(
                model=values.get("KERNELCODER_MODEL", "gpt-5.5"),
                api_key=values.get("KERNELCODER_API_KEY", ""),
                base_url=values.get("KERNELCODER_BASE_URL"),
                max_tokens=int(values.get("KERNELCODER_MAX_TOKENS", "4096")),
                temperature=float(values.get("KERNELCODER_TEMPERATURE", "0")),
                max_context_tokens=int(values.get("KERNELCODER_MAX_CONTEXT", "128000")),
                provider=values.get("KERNELCODER_PROVIDER", "openai"),
            )
        except ValueError:
            raise ValueError("Invalid numeric model configuration (tokens, temperature or context).") from None
