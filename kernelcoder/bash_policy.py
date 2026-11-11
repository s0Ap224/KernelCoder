"""Conservative shell analysis for permission decisions. Never executes input."""
import re
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class BashAnalysis:
    commands: tuple[tuple[str, ...], ...] = ()
    reusable: bool = False
    deny: str = ""
    ask: str = ""


def analyze_command(command, *, dialect="posix"):
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        return BashAnalysis(deny="Command must be a nonempty string without NUL")
    if re.search(r":\(\)\s*\{.*:\|:.*\}", command):
        return BashAnalysis(deny="Fork bomb is prohibited")
    if dialect != "posix":
        return BashAnalysis(ask="Confirm the complete command for this shell dialect")
    # Shell expansion and nested execution require approval of the whole input.
    complex_syntax = bool(re.search(r"[$`<>\\(){}*?\[\]~#]", command))
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return BashAnalysis(ask="Shell syntax could not be parsed")
    commands, current = [], []
    for token in tokens:
        if token and all(c in ";&|\n" for c in token):
            if not current or token not in (";", "&&", "||", "|", "\n"):
                complex_syntax = True
            if current:
                commands.append(tuple(current))
                current = []
        else:
            current.append(token)
    if current:
        commands.append(tuple(current))
    elif tokens and tokens[-1] not in (";", "\n"):
        complex_syntax = True
    risk = ""
    for words in commands:
        name = words[0].rsplit("/", 1)[-1]
        if name == "mkfs" or name.startswith("mkfs."):
            return BashAnalysis(tuple(commands), deny="Formatting filesystems is prohibited")
        if name == "dd" and any(w.startswith("of=/dev/") for w in words[1:]):
            return BashAnalysis(tuple(commands), deny="Raw device writes are prohibited")
        if name == "rm":
            recursive = any(w == "--recursive" or re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", w) for w in words[1:])
            if recursive and any(w in ("/", "/*", "~", "~/", "$HOME", "${HOME}") for w in words[1:]):
                return BashAnalysis(tuple(commands), deny="Recursive deletion of root/home is prohibited")
            risk = "Confirm file deletion"
        if name in {"sh", "bash", "zsh", "cmd", "cmd.exe", "powershell", "pwsh", "eval", "source", ".",
                    "python", "python3", "node", "sudo", "env", "exec", "xargs", "find", "chmod", "chown"}:
            risk = "Confirm script execution or filesystem effects"
        if name in {"if", "for", "while", "case", "function", "until", "!"} or "=" in words[0]:
            complex_syntax = True
    if complex_syntax:
        risk = "Confirm complete command containing expansions or unsupported shell structure"
    return BashAnalysis(tuple(commands), bool(commands) and not risk, ask=risk)
