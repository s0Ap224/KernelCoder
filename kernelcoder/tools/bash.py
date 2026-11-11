"""Bash facade with prepared execution, bounded output and shared hard checks."""

import os
import re
import subprocess
import threading
from typing import ClassVar

from ..bash_policy import analyze_command
from ..sandbox import SandboxRunner
from ..shell import prepare_shell, run_shell
from ..workspace import CURRENT_WORKSPACE
from .base import Tool

# Track cwd across commands (Claude Code does this too). Thread-local, so that
# when the agent executes tools in parallel two bash calls never race on one
# shared global: each worker thread carries its own cwd. See article 05.
_local = threading.local() #线程本地存储，保证并行线形程操作时，不会出现不同线程里同时读写这同一个 _cwd的问题。


def get_tracked_cwd() -> str | None:
    """The cwd tracked for this thread, if any."""
    return getattr(_local, "cwd", None) #读取当前线程之前"记录/跟踪"下来的 cwd


def set_tracked_cwd(path: str | None) -> None:
    """Set the tracked cwd for this thread (session handoff at pool edges)."""
    if path is None:
        if hasattr(_local, "cwd"):
            del _local.cwd
    else:
        _local.cwd = path

class BashTool(Tool):
    def __init__(self, workspace=None, runner=None, *, unsafe=False):
        self.workspace = workspace
        self.runner = runner or SandboxRunner()
        self.unsafe = unsafe
    # workspace:工作区对象，保存项目副本位置、基线、锁等信息
    # runner:执行器对象，负责处理沙箱命令执行
    # unsafe:是否允许在没有工作区时直接执行Shell命令

    name = "bash"
    description = (
        "Execute a shell command. Returns stdout, stderr, and exit code. "
        "Use this for running tests, installing packages, git operations, etc."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120)",
            },
            "cwd": {"type": "string",
                    "description": "Directory inside /workspace; defaults to /workspace each call"
                    },
        },
        "required": ["command"],
    }

    def prepare(self, arguments):
        command = arguments.get("command")
        timeout = arguments.get("timeout", 120)
        cwd = arguments.get("cwd")
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            raise ValueError("Command must be a nonempty string without NUL")
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("timeout must be an integer between 1 and 600")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("cwd must be a string")
        workspace = CURRENT_WORKSPACE.get() or self.workspace
        if workspace:
            directory = workspace.path(cwd or "/workspace")
            if not directory.is_dir():
                raise ValueError("cwd must be a directory")
            cwd = "/workspace" + ("/" + directory.relative_to(workspace.tree).as_posix()
                                  if directory != workspace.tree else "")
            prepared = None
        else:
            cwd = os.path.abspath(cwd or get_tracked_cwd() or os.getcwd())
            if not os.path.isdir(cwd):
                raise ValueError("cwd must be a directory")
            prepared = prepare_shell(command)
            command = prepared.command
        return {"command": command, "timeout": timeout, "cwd": cwd}, prepared

    def execute(self, command: str, timeout: int = 120, cwd: str | None = None) -> str:
        arguments, prepared = self.prepare({"command": command, "timeout": timeout, "cwd": cwd})
        return self.execute_prepared(**arguments, prepared=prepared)

    def execute_prepared(self, command, timeout=120, cwd=None, *, prepared=None):
        workspace = CURRENT_WORKSPACE.get() or self.workspace

        if workspace is not None:
            warning = _check_dangerous(command) if isinstance(command, str) else None #当command 为字符串时进行检查
            if warning:
                return f"[policy_denied] Dangerous operation: {warning}"
            result = self.runner.run(workspace, command, timeout, cwd)
            if result.status == "cancelled":
                raise KeyboardInterrupt
            return result.render()
        if not self.unsafe:
            return "[policy_denied] No sandbox workspace configured. No host fallback. Use the CLI or explicitly choose unsafe mode."
        # safety check
        warning = _check_dangerous(command)
        if warning:
            return f"⚠ Blocked: {warning}\nCommand: {command}\nIf intentional, modify the command to be more specific."

        # 以下部分均为unsafe 宿主执行分支，未实现沙箱隔离。
        # use this thread's own tracked working directory
        cwd = cwd or get_tracked_cwd() or os.getcwd()

        try:
            proc = run_shell(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
                prepared=prepared,
            )

            # track cd commands so next command runs in the right place
            if proc.returncode == 0: #退出码为0，成功退出
                _update_cwd(command, cwd)

            out = proc.stdout
            if proc.stderr:
                out += f"\n[stderr]\n{proc.stderr}"
            if proc.returncode != 0:
                out += f"\n[exit code: {proc.returncode}]"
            # keep head + tail to preserve the most useful info
            if len(out) > 15_000:
                out = (
                    out[:6000]
                    + f"\n\n... truncated ({len(out)} chars total) ...\n\n"
                    + out[-3000:]
                )
            return out.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: timed out after {timeout}s"
        except Exception as e:  # noqa: BLE001
            # anything else from the OS (spawn failure etc.) also comes back as text
            return f"Error running command: {e}"


def _check_dangerous(cmd: str) -> str | None:
    return analyze_command(cmd).deny or None


def _split_words(statement: str) -> list[str]:
    """Split one statement into words on unquoted whitespace, stripping the
    quotes. Backslashes are kept verbatim (Windows paths must not lose them,
    so this deliberately does not use shlex's posix escape processing)."""
    words: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in statement:
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if buf:
                words.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        words.append("".join(buf))
    return words


def _split_statements(command: str) -> list[list[str]]:
    """Split a shell command into per-statement word lists on `&&` and `;`,
    honoring quotes. Words come back dequoted, so `cd "my dir"` arrives as
    ["cd", "my dir"] and a separator inside quotes never splits."""
    statements: list[str] = []
    quote: str | None = None
    start = 0
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        else:
            is_and = command[i : i + 2] == "&&"
            if ch == ";" or is_and:
                statement = command[start:i]
                if statement.strip():
                    statements.append(statement)
                i += 2 if is_and else 1
                start = i
                continue
        i += 1
    tail = command[start:]
    if tail.strip():
        statements.append(tail)
    return [_split_words(statement) for statement in statements]


def _update_cwd(command: str, current_cwd: str):
    """Track directory changes from cd commands, per thread."""
    # walk each cd in a && or ; chain, resolving relative targets against the dir the
    # previous cd landed in (not the original cwd) so `cd a && cd b` ends in a/b.
    # a parenthesized group is a subshell — `( cd a )` never changes this shell's
    # cwd, so scrub those before scanning; a bare `cd` goes home.
    scrubbed = re.sub(r"\([^()]*\)", " ", command)
    running = current_cwd
    changed = False
    for words in _split_statements(scrubbed):
        if words and words[0] == "cd":
            target = words[1] if len(words) > 1 else "~"
            new_dir = os.path.normpath(os.path.join(running, os.path.expanduser(target)))
            if os.path.isdir(new_dir):
                running = new_dir
                changed = True
    if changed:
        _local.cwd = running
