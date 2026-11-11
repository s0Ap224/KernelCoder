"""Ordered permission decisions and project-scoped user consent."""
import inspect
import json
import os
import tempfile
import threading
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

from .bash_policy import analyze_command

DEFAULT_STORE = Path.home() / ".kernelcoder" / "permissions.json"


@dataclass(frozen=True)
class PermissionRule:
    behavior: str
    tool_name: str
    content: str | None = None
    match: str = "exact"
    cwd: str | None = None
    execution: str | None = None

    def __post_init__(self):
        if self.behavior not in {"allow", "deny", "ask"} or self.match not in {"exact", "prefix"}:
            raise ValueError("Invalid permission rule")
        if not isinstance(self.tool_name, str) or not self.tool_name:
            raise ValueError("Rule requires a tool name")
        if self.content is not None and (not isinstance(self.content, str) or not self.content.strip()):
            raise ValueError("Rule content must be a nonempty string")


@dataclass(frozen=True)
class PermissionDecision:
    behavior: str
    reason: str
    source: str
    matched_rule: PermissionRule | None = None
    reusable: bool = False


def trusted_read(tool):
    from .skills import SkillTool
    from .tools.glob_tool import GlobTool
    from .tools.grep import GrepTool
    from .tools.read import ReadFileTool
    from .tools.todo import TodoWriteTool
    from .tools.transcript import ReadTranscriptTool
    return type(tool) in {ReadFileTool, GlobTool, GrepTool, TodoWriteTool, ReadTranscriptTool, SkillTool}


class Permission:
    READ_ONLY = frozenset({"read_file", "glob", "grep", "todo_write", "load_skill", "read_transcript"})
    PARALLEL_READ_ONLY = frozenset({"read_file", "glob", "grep", "load_skill"})

    def __init__(self, ask=None, allow_all=False, persist_path=None, *, mode="default", project_root=None, rules=()):
        self.mode = "bypass" if allow_all else mode
        if self.mode not in {"default", "acceptEdits", "bypass"}:
            raise ValueError("Invalid permission mode")
        self.allow_all = self.mode == "bypass"
        self.ask = ask
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.project_key = os.path.normcase(str(self.project_root))
        self._persist_path = Path(persist_path) if persist_path else None
        self._lock = threading.RLock()
        self.rules = list(rules)
        self._session = []
        self._saved = []
        self._store = {"version": 2, "projects": {}}
        self._store_text = None
        if self._persist_path and self._persist_path.exists():
            try:
                self._store_text = self._persist_path.read_text(encoding="utf-8")
                saved = json.loads(self._store_text)
                if "always" in saved and "version" not in saved:
                    warnings.warn("Legacy always permissions retained but inactive; approve project rules again.", stacklevel=2)
                    self._store["legacy"] = saved
                else:
                    if saved.get("version") != 2 or not isinstance(saved["projects"], dict):
                        raise ValueError("Unsupported permission store")
                    for record in saved["projects"].values():
                        for rule in record["rules"]:
                            PermissionRule(**rule)
                    self._store = saved
                    self._saved = [PermissionRule(**r) for r in saved["projects"].get(self.project_key, {}).get("rules", [])]
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise ValueError(f"Invalid permission store: {self._persist_path}") from exc

    def evaluate(self, tool, arguments, *, context=None):
        from .tools.bash import BashTool
        from .tools.edit import EditFileTool
        from .tools.write import WriteFileTool
        context = context or {}
        name = context.get("permission_key", getattr(tool, "name", tool))
        read = trusted_read(tool)
        if context.get("plan_mode") and not (read or context.get("managed_read", False)):
            return PermissionDecision("deny", "Plan mode is on: read-only. Type approve or /plan to resume execution", "plan")
        analysis = None
        if type(tool) is BashTool or name == "bash":
            analysis = analyze_command(arguments.get("command"), dialect=context.get("dialect", "posix"))
            if analysis.deny:
                return PermissionDecision("deny", analysis.deny, "safety")
        rules = [*self.rules, *self._saved, *self._session]
        def matches(rule):
            if rule.cwd is not None and rule.cwd != context.get("cwd"):
                return False
            if rule.execution is not None and rule.execution != context.get("execution"):
                return False
            if rule.tool_name != name:
                return False
            if rule.content is None:
                return True
            if analysis is None:
                return False
            if rule.match == "exact" and rule.content == arguments.get("command"):
                return True
            pattern = analyze_command(rule.content)
            if not pattern.reusable or len(pattern.commands) != 1:
                return False
            expected = pattern.commands[0]
            return any(words == expected if rule.match == "exact" else words[:len(expected)] == expected
                       for words in analysis.commands)
        for behavior in ("deny", "ask"):
            for rule in rules:
                if rule.behavior == behavior and matches(rule):
                    return PermissionDecision(behavior, f"Matched {behavior} rule", "rule", rule)
        if analysis and analysis.ask:
            return PermissionDecision("ask", analysis.ask, "safety")
        if type(tool) in {EditFileTool, WriteFileTool}:
            path = context.get("path")
            if path is None:
                path = Path(arguments.get("file_path", "")).resolve()
            root = Path(context.get("tree", self.project_root)).resolve()
            path = Path(path).resolve()
            if not path.is_relative_to(root) or any(
                part in {".git", ".ssh", ".aws", ".kernelcoder", ".claude", ".vscode"} or part.startswith(".env")
                for part in path.parts
            ):
                return PermissionDecision("ask", "Confirm editing a sensitive or external path", "safety")
            if self.mode == "acceptEdits":
                return PermissionDecision("allow", "Project edit accepted", "mode")
        allows = [r for r in rules if r.behavior == "allow" and matches(r)]
        if analysis and allows:
            # Each component needs authorization; a matching prefix never covers its siblings.
            allowed = any(r.content is None or (r.match == "exact" and r.content == arguments["command"]) for r in allows)
            if not allowed:
                allowed = all(any(self._matches_words(r, words) for r in allows) for words in analysis.commands)
        else:
            allowed = bool(allows)
        if allowed or read or self.mode == "bypass":
            return PermissionDecision("allow", "Allowed by rule, trusted tool or mode", "rule" if allowed else "mode")
        reusable = bool(analysis and analysis.reusable)
        return PermissionDecision("ask", "No applicable permission", "default", reusable=reusable)

    @staticmethod
    def _matches_words(rule, words):
        pattern = analyze_command(rule.content or "")
        if len(pattern.commands) != 1:
            return False
        expected = pattern.commands[0]
        return words == expected if rule.match == "exact" else words[:len(expected)] == expected

    def authorize(self, tool, arguments, *, context=None):
        with self._lock:
            decision = self.evaluate(tool, arguments, context=context)
            name = getattr(tool, "name", tool)
            if decision.behavior == "allow":
                return None
            if decision.behavior == "deny":
                return f"Permission denied: {decision.reason}"
            if self.ask is None:
                return f"Permission denied: non-interactive session cannot approve {name}: {decision.reason}"
            try:
                # Keep existing embedding callbacks usable while the CLI receives full detail.
                if "decision" in inspect.signature(self.ask).parameters:
                    verdict = self.ask(name, arguments, decision=decision, context=context or {})
                else:
                    verdict = self.ask(name, arguments)
            except (EOFError, KeyboardInterrupt):
                return "Permission denied: approval interrupted"
            if verdict == "once":
                return None
            if verdict in {"session", "project", "always"} and decision.reusable:
                rule = PermissionRule("allow", name, arguments["command"],
                                      cwd=(context or {}).get("cwd"), execution=(context or {}).get("execution"))
                self._session.append(rule)
                if verdict in {"project", "always"}:
                    self._saved.append(rule)
                    try:
                        self._save()
                    except OSError as exc:
                        self._saved.remove(rule)
                        warnings.warn(f"Permission granted for this session only; save failed: {exc}", stacklevel=2)
                return None
            return "Permission denied: the user refused this call or requested an unavailable grant"

    def check(self, tool_name, arguments):
        return self.authorize(tool_name, arguments)

    def describe(self):
        return json.dumps({"project": str(self.project_root), "mode": self.mode,
                           "rules": [asdict(r) for r in [*self.rules, *self._saved, *self._session]]}, ensure_ascii=False, indent=2)

    def reset(self):
        with self._lock:
            old = self._saved
            self._saved = []
            try:
                self._save()
            except OSError:
                self._saved = old
                raise
            self._session.clear()

    def _save(self):
        if self._persist_path is None:
            raise OSError("No permission store configured")
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize writers across CLI processes and reject stale policy snapshots.
        lock_path = self._persist_path.with_suffix(".lock")
        with lock_path.open("a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                current = self._persist_path.read_text(encoding="utf-8") if self._persist_path.exists() else None
                if current != self._store_text:
                    raise OSError("Permission store changed in another process; reload before saving")
                updated = dict(self._store)
                updated["projects"] = dict(self._store["projects"])
                updated["projects"][self.project_key] = {
                    "path": str(self.project_root), "rules": [asdict(r) for r in self._saved],
                }
                text = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
                fd, temporary = tempfile.mkstemp(dir=self._persist_path.parent, prefix="permissions-", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(text)
                    os.replace(temporary, self._persist_path)
                    self._store = updated
                    self._store_text = text
                finally:
                    Path(temporary).unlink(missing_ok=True)
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)
