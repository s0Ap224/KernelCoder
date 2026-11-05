"""Curated, lazy local MCP jobs over a frozen managed workspace.

No E2B imports or connection checks belong in this module. Git's disposable
baseline view is deliberately not the persistent task history planned next.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .mcp import MCPClient, MCPError
from .tools.base import Tool
from .workspace import WorkspaceError, atomic_write, checked_path, fingerprint, read_regular

DATA = Path(__file__).with_name("mcp_data")
PINS = {"semgrep": "1.181.0", "mcp-server-git": "2026.8.18", "mcp": "1.29.0"}
GIT_TOOLS = ("git_status", "git_diff_unstaged", "git_log", "git_show")


def server_args(name):
    if name == "git":
        return ["-I", "-m", "mcp_server_git"]
    # `python -m semgrep` is a retired entry point; call its installed console entry.
    return ["-I", "-c", "from semgrep.console_scripts.entrypoint import main; main()", "mcp"]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def revision(manifest):
    # Windows modes are permission emulation, not executable bits.
    files = {name: {**entry, "mode": 0o755 if os.name != "nt" and entry["mode"] & 0o111 else 0o644}
             for name, entry in manifest.items()}
    return "sha256:" + digest({"version": 1, "files": files})


@dataclass(frozen=True)
class MCPServerProfile:
    name: str
    package: str
    version: str
    tools: dict


@dataclass(frozen=True)
class MCPJob:
    task_id: str
    server: str
    tool: str
    arguments: dict
    revision_id: str
    profile_digest: str = ""
    rules_digest: str | None = None
    backend: str = "local"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class MCPJobResult:
    job: MCPJob
    status: str = "running"
    result: dict = field(default_factory=dict)
    detail: str = ""
    stderr: str = ""
    elapsed_seconds: float = 0
    output_revision_id: str | None = None
    runtime: dict = field(default_factory=dict)

    def render(self):
        content = json.dumps(self.result, ensure_ascii=False)
        if len(content) > 16000:
            content = content[:16000] + "\n[display truncated; full result in job record]"
        return f"[{self.status}; local; {self.job.revision_id}; job={self.job.id}]\n{self.detail}\n{content}"


def clean_environment(home, python):
    """Keep OS necessities, not LLM/E2B keys, user Git config, or Python hooks."""
    names = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "LANG", "LC_ALL")
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    env.update({
        "PATH": str(Path(python).parent) + os.pathsep + env.get("PATH", os.defpath),
        "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home),
        "LOCALAPPDATA": str(home), "TMP": str(home), "TEMP": str(home), "TMPDIR": str(home),
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
        "SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0",
        "SEMGREP_USER_AGENT_APPEND": "KernelCoder", "USE_SEMGREP_RPC": "false",
    })
    return env


def _copy_manifest(root, target, manifest):
    for name, expected in manifest.items():
        data, mode = read_regular(checked_path(root, name))
        if fingerprint(data, mode) != expected:
            raise WorkspaceError(f"Input changed while freezing: {name}")
        atomic_write(checked_path(target, name), data, mode)


class LocalMCPRunner:
    def __init__(self, workspace, python, profiles):
        self.workspace = workspace
        self.python = str(Path(python).absolute())
        self.profiles = profiles
        self.last_result = None

    def _versions(self, env):
        code = ("import importlib.metadata as m,json,platform;"
                f"print(json.dumps({{'packages':{{p:m.version(p) for p in {list(PINS)!r}}},"
                "'python':platform.python_version(),'platform':platform.platform()}))")
        proc = subprocess.run([self.python, "-I", "-c", code], check=False, env=env, capture_output=True,
                              text=True, encoding="utf-8", timeout=15)
        if proc.returncode:
            raise MCPError("Pinned MCP environment is unavailable; install requirements/mcp-local.txt")
        runtime = json.loads(proc.stdout)
        if runtime["packages"] != PINS:
            raise MCPError(f"MCP package version mismatch; expected {PINS}")
        return runtime

    def _git_view(self, root, manifest, env):
        ws = self.workspace
        git = shutil.which("git")
        if not git:
            raise MCPError("Git executable is missing")
        def run(*args):
            proc = subprocess.run([git, "-C", str(root), *args], check=False, env=env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=30)
            if proc.returncode:
                raise MCPError(f"Cannot build task Git view: {proc.stderr[-2000:]}")
        run("init", "--quiet", "--initial-branch=task")
        run("config", "core.autocrlf", "false")
        run("config", "core.hooksPath", str(root / ".git" / "empty-hooks"))
        # info/attributes overrides project attributes, including clean filters.
        (root / ".git" / "info" / "attributes").write_text(
            "* -text -filter -ident -working-tree-encoding\n", encoding="utf-8")
        _copy_manifest(ws.baseline, root, ws.meta["baseline"])
        run("add", "--force", "--all", "--", ".")
        run("-c", "user.name=KernelCoder", "-c", "user.email=task@localhost",
            "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Task initial baseline")
        parents = set()
        for name in ws.meta["baseline"]:
            path = checked_path(root, name)
            path.chmod(0o644)
            path.unlink()
            parents.update(p for p in path.parents if p != root and root in p.parents)
        for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
            parent.rmdir()
        _copy_manifest(ws.tree, root, manifest)

    def run(self, server, tool, arguments, timeout=60):
        ws = self.workspace
        if server not in self.profiles or tool not in self.profiles[server].tools:
            raise ValueError("MCP tool is not in the curated allowlist")
        if server == "git" and arguments:
            raise ValueError("Git task tool accepts no overrides")
        if server == "semgrep" and (set(arguments) != {"paths"} or not isinstance(arguments["paths"], list)
                                   or not arguments["paths"] or
                                   not all(isinstance(p, str) for p in arguments["paths"])):
            raise ValueError("Expected a nonempty list of task-relative paths")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 600:
            raise ValueError("MCP timeout must be in (0, 600]")
        with ws.guard():
            manifest = ws.scan()
            rules = (DATA / "security.yaml").read_bytes() if server == "semgrep" else None
            job = MCPJob(ws.id, server, tool, arguments, revision(manifest),
                         digest(asdict(self.profiles[server])),
                         hashlib.sha256(rules).hexdigest() if rules else None)
            result = MCPJobResult(job)
            directory = ws.directory / "mcp-jobs" / job.id
            directory.mkdir(parents=True)
            atomic_write(directory / "input-manifest.json", json.dumps(manifest, sort_keys=True).encode())
            atomic_write(directory / "result.json", json.dumps(asdict(result)).encode())
            start = time.monotonic()
            client = None
            try:
                # Windows Semgrep uses AF_UNIX sockets under TMP. A deeply
                # nested task path can exceed the socket path limit. OS temp
                # also avoids inheriting the source repo's .gitignore rules.
                with tempfile.TemporaryDirectory(prefix="ccm-") as temp, ExitStack() as processes:
                    root = Path(temp).resolve()  # Canonicalize OS temp aliases (macOS /var -> /private/var).
                    home, view = root / "home", root / "input"
                    home.mkdir()
                    view.mkdir()
                    env = clean_environment(home, self.python)
                    result.runtime = self._versions(env)
                    profile = self.profiles[server]
                    if server == "git":
                        self._git_view(view, manifest, env)
                        args = [*server_args("git"), "--repository", str(view)]
                        params = {"repo_path": str(view), **arguments}
                        if tool == "git_show":
                            params["revision"] = "HEAD"
                    else:
                        _copy_manifest(ws.tree, view, manifest)
                        files = arguments["paths"]
                        code = []
                        for name in files:
                            if name not in manifest:
                                raise WorkspaceError(f"Scan target not in task manifest: {name}")
                            data = checked_path(view, name).read_bytes()
                            code.append({"path": name, "content": data.decode("utf-8")})
                        args = server_args("semgrep")
                        params = {"code_files": code, "rule": rules.decode("utf-8")}
                        env["SEMGREP_MCP_DISABLE_TRACING"] = "true"
                    client = MCPClient(server, self.python, args, env, cwd=view, managed=True)
                    processes.callback(client.close)
                    actual = {s["name"]: s["inputSchema"] for s in client.tool_specs}
                    if any(actual.get(name) != schema for name, schema in profile.tools.items()):
                        raise MCPError("Pinned tools/list schema mismatch")
                    client.call_timeout = timeout
                    result.result = client.call_result(tool, params)
                    result.status = "tool_error" if result.result.get("isError") else "completed"
                    client.close()
                    result.stderr = client.stderr
                    client = None
            except KeyboardInterrupt:
                result.status = "cancelled"
                raise
            except (OSError, ValueError, MCPError, WorkspaceError, subprocess.SubprocessError) as e:
                result.status = "timeout" if "gave no answer" in str(e) else "error"
                result.detail = str(e)
            finally:
                if client:
                    client.close()
                    result.stderr = client.stderr
                result.elapsed_seconds = round(time.monotonic() - start, 3)
                result.output_revision_id = revision(ws.scan())
                if result.output_revision_id != job.revision_id:
                    result.status = "input_changed"
                self.last_result = result
                atomic_write(directory / "result.json", json.dumps(asdict(result), ensure_ascii=False, indent=2).encode())
            return result


class ManagedMCPTool(Tool):
    def __init__(self, manager, server, remote_name):
        self.manager, self.server, self.remote_name = manager, server, remote_name
        self.name = f"mcp__{server}__{'scan' if server == 'semgrep' else remote_name}"
        self.description = (
            "Read the task copy relative to its initial baseline using local Git; only the baseline has history."
            if server == "git" else
            "Scan UTF-8 task files locally with the bundled fixed rules. This is a limited static check, not test evidence."
        )
        self.parameters = {"type": "object", "properties": {}, "additionalProperties": False}
        if server == "semgrep":
            self.parameters.update(properties={"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
                                   required=["paths"])

    def execute(self, **kwargs):
        if self.server == "git" and kwargs:
            raise ValueError("Git task tool accepts no path or revision overrides")
        if self.server == "semgrep":
            if set(kwargs) != {"paths"} or not isinstance(kwargs["paths"], list) or not kwargs["paths"]:
                raise ValueError("paths must be a nonempty list of task-relative UTF-8 files")
            if not all(isinstance(p, str) for p in kwargs["paths"]):
                raise ValueError("paths must contain strings")
            kwargs = {"paths": sorted(set(kwargs["paths"]))}
        return self.manager.runner.run(self.server, self.remote_name, kwargs).render()

    @property
    def permission_key(self):
        m = self.manager
        return f"{self.name}@local:{m.workspace.id}:{m.profile_digest}"


class MCPManager:
    def __init__(self, workspace, python=sys.executable):
        self.workspace = workspace
        data = json.loads((DATA / "profiles.json").read_text(encoding="utf-8"))
        self.profile_digest = digest({"profiles": data, "python": str(Path(python).absolute()),
                                      "rules": (DATA / "security.yaml").read_text(encoding="utf-8")})
        self.profiles = {name: MCPServerProfile(name, spec["package"], spec["version"], spec["tools"])
                         for name, spec in data.items()}
        self.runner = LocalMCPRunner(workspace, python, self.profiles)
        self.tools = [ManagedMCPTool(self, "git", name) for name in GIT_TOOLS]
        self.tools.append(ManagedMCPTool(self, "semgrep", "semgrep_scan_with_custom_rule"))

    def status(self):
        last = self.runner.last_result
        return (f"MCP backend: local (host process); task: {self.workspace.id}\n"
                f"Python: {self.runner.python}\n"
                f"Pins: {PINS}\n"
                f"Services start only on invocation; last job: {last.status if last else 'not started'}")
