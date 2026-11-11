"""Shell routing: POSIX commands need a POSIX shell.

On macOS/Linux `shell=True` already means /bin/sh. On Windows it means
cmd.exe, where pwd, ls, cat, sleep and friends do not exist. Every shell
spawn in KernelCoder goes through here so Windows gets Git Bash instead.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

# Drive-letter span: `C:\Users\...` up to a shell metacharacter. Escapes like
# `\n` never follow a drive letter, so they pass through untouched.
_DRIVE_PATH_RE = re.compile(r"([A-Za-z]):((?:\\[^\s;&|<>()'\"]+)+)")


def _posixify_drive_paths(command: str) -> str:
    """Rewrite drive-letter paths to git-bash's ``C:/...`` form.

    bash's lexer eats backslashes, so an unquoted ``C:\\Users\\runner`` hook
    path or ``cd`` target comes out as ``C:Usersrunner`` and the command
    fails silently. git-bash accepts the forward-slash form natively, and no
    shell escape sequence looks like a drive letter.
    """
    return _DRIVE_PATH_RE.sub(lambda m: m.group(1) + ":" + m.group(2).replace("\\", "/"), command)


@lru_cache(maxsize=1)
def _git_bash() -> str | None:
    """Locate Git Bash on Windows; None elsewhere or when absent.

    System32\\bash.exe is WSL (a different filesystem and environment, not
    the Git installation the user's commands target), so it is never picked.
    """
    if os.name != "nt":
        return None
    candidate = os.path.join(
        os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"
    )
    if os.path.isfile(candidate):
        return candidate
    git = shutil.which("git")
    if git:
        derived = os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe")
        if os.path.isfile(derived):
            return derived
    return None


@dataclass(frozen=True)
class PreparedShell:
    command: str
    executable: str
    dialect: str


def prepare_shell(command):
    bash = _git_bash()
    if bash:
        return PreparedShell(_posixify_drive_paths(command), bash, "posix")
    if os.name == "nt":
        return PreparedShell(command, os.environ.get("COMSPEC", "cmd.exe"), "cmd")
    return PreparedShell(command, "/bin/sh", "posix")


def run_shell(command: str, check: bool = False, *, prepared=None, **kwargs):
    if prepared is not None and prepared.command != command:
        raise ValueError("Prepared shell command changed after approval")
    prepared = prepared or prepare_shell(command)
    if prepared.dialect == "cmd":
        return subprocess.run([prepared.executable, "/d", "/s", "/c", prepared.command],
                              shell=False, check=check, **kwargs)
    return subprocess.run([prepared.executable, "-c", prepared.command], shell=False, check=check, **kwargs)
