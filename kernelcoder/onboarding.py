"""First-run setup and offline diagnostics for the CLI."""

import getpass
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import venv
from pathlib import Path

from .config import ALIASES, Config, canonical, load_settings, read_env, resolved_environment, user_dir


def write_private(path, text):
    """Replace a config atomically; temporary credentials are owner-only on POSIX."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_settings(settings):
    write_private(user_dir() / "settings.json", json.dumps(settings, ensure_ascii=False, indent=2) + "\n")


def _ask(label, current="", *, secret=False, clear=False):
    default = "configured; Enter keeps it" if secret and current else ("empty" if secret else current or "empty")
    suffix = "; '-' clears" if clear else ""
    reader = getpass.getpass if secret else input
    answer = reader(f"{label} [{default}{suffix}]: ").strip()
    return "" if clear and answer == "-" else answer or current


def initialize(console, from_env=None, mode=None):
    path = user_dir() / ".env"
    existing = read_env(path)
    values = canonical(existing)
    if from_env:
        values.update(canonical(read_env(from_env, required=True)))
    settings = load_settings()
    selected = mode or _ask("Execution mode: local / e2b", settings.get("mode", "e2b"))
    if selected not in ("local", "e2b"):
        raise ValueError("Execution mode must be local or e2b.")
    values["KERNELCODER_MODEL"] = _ask("Model", values.get("KERNELCODER_MODEL", "gpt-5.5"))
    values["KERNELCODER_BASE_URL"] = _ask("API base URL (empty uses provider default)",
                                         values.get("KERNELCODER_BASE_URL", ""), clear=True)
    values["KERNELCODER_API_KEY"] = _ask("Model API key", values.get("KERNELCODER_API_KEY", ""), secret=True)
    if not values["KERNELCODER_API_KEY"]:
        raise ValueError("A model API key is required; no settings were saved.")
    if selected == "e2b":
        values["E2B_API_KEY"] = _ask("E2B API key", values.get("E2B_API_KEY", ""), secret=True)
        if not values["E2B_API_KEY"]:
            raise ValueError("An E2B API key is required for E2B setup; no settings were saved.")
    for name, convert in (("KERNELCODER_MAX_TOKENS", int), ("KERNELCODER_MAX_CONTEXT", int),
                          ("KERNELCODER_TEMPERATURE", float)):
        if name in values:
            try:
                convert(values[name])
            except ValueError:
                raise ValueError(f"Invalid {name}; no settings were saved.") from None
    console.print(f"Default mode: {selected}; credentials: configured (hidden).", markup=False)
    for name in sorted(values):
        console.print(f"  {name}: {'configured' if values[name] else 'empty'}", markup=False)
    if ((path.exists() or (user_dir() / "settings.json").exists())
            and input("Replace saved configuration with the summary above? [y/N]: ").strip().lower() not in ("y", "yes")):
        console.print("Cancelled; configuration unchanged.")
        return
    # Preserve unrelated user entries, but normalize all recognized aliases.
    supported = {alias for aliases in ALIASES.values() for alias in aliases}
    output = {key: value for key, value in existing.items() if key not in supported and value is not None}
    output.update(values)
    def quote(value):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    write_private(path, "".join(f"{key}={quote(value)}\n" for key, value in output.items()))
    settings["mode"] = selected
    save_settings(settings)
    console.print("Saved to ~/.kernelcoder. Open a terminal in your project and run kernelcoder.")


def doctor(console, args, settings):
    values, sources = resolved_environment()
    config = Config.from_env()
    checks = [("Model API key", bool(args.api_key or config.api_key))]
    console.print(f"Project: {Path.cwd()}\nExecution: {'local host' if args.sandbox == 'unsafe' else 'E2B Linux'}",
                  markup=False)
    for name, source in sorted(sources.items()):
        console.print(f"{name}: configured; source: {source}", markup=False)
    for name, value in (("KERNELCODER_MODEL", args.model), ("KERNELCODER_BASE_URL", args.base_url),
                        ("KERNELCODER_API_KEY", args.api_key)):
        if value:
            console.print(f"{name}: configured; effective source: CLI", markup=False)
    if args.sandbox == "unsafe":
        if os.name == "nt":
            from .shell import _git_bash
            checks.append(("Git Bash (install Git for Windows if missing)", bool(_git_bash())))
        else:
            checks.append(("POSIX shell", Path("/bin/sh").is_file()))
    else:
        checks.extend([("E2B API key", bool(values.get("E2B_API_KEY"))),
                       ("E2B SDK", importlib.util.find_spec("e2b") is not None)])
        mcp = settings.get("mcp", {})
        backend = args.mcp_backend or mcp.get("backend", "off")
        if backend == "local":
            python = args.mcp_python or mcp.get("python")
            checks.extend([("MCP pinned environment (run kernelcoder setup-mcp)",
                            bool(python) and mcp_valid(Path(python))),
                           ("Git executable", bool(shutil.which("git")))])
    if args.sandbox == "unsafe" and (args.mcp_backend == "local" or args.mcp_python):
        checks.append(("Managed MCP requires E2B task mode", False))
    if args.mcp_python and args.mcp_backend == "off":
        checks.append(("--mcp-python requires local MCP", False))
    for label, ok in checks:
        console.print(f"{'OK' if ok else 'MISSING'}: {label}", markup=False)
    console.print("Offline checks only; no model or E2B connection was attempted.")
    return 0 if all(ok for _, ok in checks) else 1


def mcp_valid(python):
    from .mcp_local import PINS
    if not python.is_file():
        return False
    code = "import importlib.metadata as m,json; print(json.dumps({n:m.version(n) for n in " + repr(list(PINS)) + "}))"
    try:
        result = subprocess.run([str(python), "-I", "-c", code], check=False, capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and json.loads(result.stdout) == PINS
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def setup_mcp(console):
    from .mcp_local import PINS
    settings = load_settings()
    directory = user_dir() / "mcp-env"
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not mcp_valid(python):
        console.print("Preparing the pinned MCP environment; this may take a few minutes.")
        venv.EnvBuilder(with_pip=True).create(directory)
        result = subprocess.run([str(python), "-m", "pip", "install", *[f"{name}=={version}" for name, version in PINS.items()]],
                                check=False, capture_output=True, text=True)
        if result.returncode or not mcp_valid(python):
            raise ValueError("MCP installation/verification failed; saved settings unchanged. Check Python compatibility/network.")
    settings["mcp"] = {"backend": "local", "python": str(python.absolute())}
    save_settings(settings)
    console.print("MCP ready and enabled for E2B tasks. Local host mode keeps managed MCP disabled.")
