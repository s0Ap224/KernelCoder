"""Host-owned session metadata and an untrusted project copy.

Only ``tree`` may be sent to an execution backend. Baselines, review candidates,
checkpoints and journals stay local. File tools and execution share the lock.
"""

import base64
import contextvars
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

CURRENT_WORKSPACE = contextvars.ContextVar("workspace", default=None)
DEFAULT_STORE = Path.home() / ".kernelcoder" / "sandboxes"
EXCLUDES = (
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".tmp",
    ".env*", ".ssh", ".aws", ".azure", ".config", ".gnupg", ".kube",
    ".docker", ".codex", ".agents", ".kernelcoder", ".kernelcoder_history",
    # Never copy credentials/history left by earlier installations either.
    ".corecoder", ".corecoder_history",
    "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "credentials*", ".netrc", ".npmrc", ".pypirc",
)
MAX_FILE = 32 * 1024 * 1024
MAX_TREE = 256 * 1024 * 1024
MAX_FILES = 20_000


class WorkspaceError(RuntimeError):
    pass


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _parts(name):
    value = PurePosixPath(name.replace("\\", "/"))
    if value.is_absolute():
        raise WorkspaceError(f"Absolute path refused: {name}")
    parts = value.parts
    if any(p in ("/", "..") or ":" in p or p.endswith((".", " ")) or any(ord(c) < 32 or ord(c) == 127 for c in p)
                        or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", p, re.IGNORECASE)
                        for p in parts):
        raise WorkspaceError(f"Unsafe relative path: {name}")
    return parts


def checked_path(root, name):
    """Check every component without resolving away a malicious link."""
    root = Path(root)
    for parent in (root, *root.parents):
        if _linked(parent.lstat()):
            raise WorkspaceError(f"Linked workspace ancestor: {parent}")
    p = root
    for part in _parts(name):
        p = p / part
        try:
            info = p.lstat()
        except FileNotFoundError:
            continue
        if _linked(info) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise WorkspaceError(f"Link or special file refused: {name}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise WorkspaceError(f"Hard link refused: {name}")
        if stat.S_ISREG(info.st_mode) and info.st_size > MAX_FILE:
            raise WorkspaceError(f"Oversized file refused: {name}")
    return p


def read_regular(p):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(p, flags), "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
            raise WorkspaceError(f"Non-regular or oversized file: {p.name}")
        data = f.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise WorkspaceError(f"Oversized file: {p.name}")
        return data, stat.S_IMODE(info.st_mode) & 0o777


def fingerprint(data, mode):
    return {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode, "size": len(data)}


def atomic_write(p, data, mode=0o644):
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_name(f".kernelcoder-{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temp.chmod(mode)
        os.replace(temp, p)
    finally:
        temp.unlink(missing_ok=True)


class SandboxWorkspace:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.tree = self.directory / "tree"
        self.baseline = self.directory / "baseline"
        self._lock = threading.RLock()
        self._depth = 0
        self._load()
        self.original = Path(self.meta["original"])
        self.id = self.directory.name

    @classmethod
    def create(cls, original, store=None, excludes=()):
        original = Path(original).absolute()
        if not original.is_dir() or original.resolve() == original.resolve().parent:
            raise WorkspaceError("Project directory must exist and cannot be a drive/filesystem root")
        store = Path(store or DEFAULT_STORE).absolute()
        if store.resolve() == original.resolve() or original.resolve() in store.resolve().parents:
            raise WorkspaceError("Sandbox metadata store must be outside the project")
        directory = store / uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        (directory / "tree").mkdir()
        (directory / "baseline").mkdir()
        meta = {"version": 1, "original": str(original), "excludes": list(EXCLUDES) + list(excludes),
                "baseline": {}, "skipped": [], "active": None, "state": "open"}
        atomic_write(directory / "meta.json", json.dumps(meta).encode())
        ws = cls(directory)
        with ws.guard():
            files = ws.scan(original, skip=True)
            for name, entry in files.items():
                data, mode = read_regular(checked_path(original, name))
                if fingerprint(data, mode) != entry:
                    raise WorkspaceError(f"Project changed during copy: {name}; start again")
                atomic_write(ws.baseline / name, data, mode)
                atomic_write(ws.tree / name, data, mode)
            ws.meta["baseline"] = files
            ws._save()
        return ws

    @classmethod
    def open(cls, session_id, store=None):
        if not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise WorkspaceError("Invalid sandbox session ID")
        return cls(Path(store or DEFAULT_STORE).absolute() / session_id)

    def _load(self):
        self.meta = json.loads((self.directory / "meta.json").read_text(encoding="utf-8"))

    def _save(self):
        atomic_write(self.directory / "meta.json", json.dumps(self.meta, indent=2).encode())

    @contextmanager
    def guard(self, allow_active=False, allow_closed=False):
        """Thread reentrancy plus a nonblocking interprocess lock; fail closed."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                lock = (self.directory / "lock").open("a+b")
                try:
                    if os.name == "nt":
                        import msvcrt
                        lock.write(b"0")
                        lock.flush()
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as e:
                    lock.close()
                    raise WorkspaceError("Session is busy in another process") from e
            self._depth += 1
            token = CURRENT_WORKSPACE.set(self)
            try:
                if outer:
                    self._load()
                if self.meta["state"] != "open" and not allow_closed:
                    raise WorkspaceError(f"Session is {self.meta['state']}; create a new session")
                if (self.meta.get("active") or self.meta.get("sync_pending")) and not allow_active:
                    raise WorkspaceError("Unclean sandbox execution: cleanup must be confirmed before reuse")
                yield self
            finally:
                CURRENT_WORKSPACE.reset(token)
                self._depth -= 1
                if outer:
                    if os.name == "nt":
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    lock.close()

    def excluded(self, name):
        return any(fnmatch.fnmatchcase(part.lower(), pattern.lower())
                   or fnmatch.fnmatchcase(name.lower(), pattern.lower())
                   for part in PurePosixPath(name).parts for pattern in self.meta["excludes"])

    def scan(self, root=None, skip=False):
        root = Path(root or self.tree)
        files, total, count = {}, 0, 0
        checked_path(root, ".")
        stack = [root]
        while stack:
            folder = stack.pop()
            for p in sorted(folder.iterdir()):
                count += 1
                if count > MAX_FILES:
                    raise WorkspaceError("Workspace entry limit exceeded")
                name = p.relative_to(root).as_posix()
                try:
                    if self.excluded(name):
                        if skip:
                            self.meta["skipped"].append(name)
                        continue
                    checked_path(root, name)
                    if p.is_dir():
                        stack.append(p)
                        continue
                    data, mode = read_regular(p)
                except WorkspaceError:
                    if skip:
                        self.meta["skipped"].append(name)
                        continue
                    raise
                total += len(data)
                if total > MAX_TREE:
                    raise WorkspaceError("Workspace size limit exceeded (256 MiB); choose a smaller project or exclude large files")
                files[name] = fingerprint(data, mode)
        return files

    def path(self, value):
        value = str(value).replace("\\", "/")
        if value == "/workspace":
            value = "."
        elif value.startswith("/workspace/"):
            value = value[len("/workspace/"):]
        elif Path(value).is_absolute():
            p = Path(value)
            try:
                value = p.relative_to(self.original).as_posix()
            except ValueError:
                try:
                    value = p.relative_to(self.tree).as_posix()
                except ValueError as e:
                    raise WorkspaceError("Path is outside the project") from e
        if self.excluded(value):
            raise WorkspaceError(f"Excluded path: {value}")
        return checked_path(self.tree, value)

    def virtual(self, path):
        return "/workspace/" + Path(path).relative_to(self.tree).as_posix()

    def status(self):
        self._load()
        return json.dumps({"session": self.id, "copy": str(self.tree), "original": str(self.original),
                           "policy": "E2B remote execution; reviewed writeback; no host fallback",
                           **{k: v for k, v in self.meta.items() if k not in {"checkpoints", "review"}}}, indent=2)

    def review(self):
        with self.guard():
            current = self.scan()
            before = self.meta["baseline"]
            changed = {n: current.get(n) for n in sorted(before.keys() | current.keys())
                       if before.get(n) != current.get(n)}
            review_id = uuid.uuid4().hex
            frozen = self.directory / "reviews" / review_id
            frozen.mkdir(parents=True)
            preview = ""
            total_chars = 0
            diff_path = frozen.parent / f"{review_id}.diff"
            diff_path.touch(exist_ok=False)
            for name, entry in changed.items():
                old = b""
                if name in before:
                    old, old_mode = read_regular(checked_path(self.baseline, name))
                    if fingerprint(old, old_mode) != before[name]:
                        raise WorkspaceError(f"Baseline hash mismatch: {name}")
                new = b""
                if entry is not None:
                    new, mode = read_regular(self.path(name))
                    if fingerprint(new, mode) != entry:
                        raise WorkspaceError("Copy changed while freezing review")
                    atomic_write(frozen / name, new, mode)
                action = "ADD" if name not in before else "DELETE" if entry is None else "MODIFY"
                out = [f"{action} {name}\n"]
                if b"\0" in old + new or len(old) + len(new) > 256_000:
                    out.append(f"Binary/large file; inspect frozen candidate: {frozen / name}\n")
                else:
                    out.extend(difflib.unified_diff(old.decode("utf-8", "replace").splitlines(True),
                                                   new.decode("utf-8", "replace").splitlines(True),
                                                   fromfile=f"a/{name}", tofile=f"b/{name}"))
                if before.get(name, {}).get("mode") != (entry or {}).get("mode"):
                    out.append(f"\nmode: {before.get(name, {}).get('mode')} -> {(entry or {}).get('mode')}\n")
                text = "".join(out)
                total_chars += len(text)
                preview += text[:max(0, 200_000 - len(preview))]
                with diff_path.open("a", encoding="utf-8", newline="") as f:
                    f.write(text)
            if not changed:
                preview = "No changes.\n"
                atomic_write(diff_path, preview.encode())
            self.meta["review"] = {"id": review_id, "changes": changed, "tree": current}
            atomic_write(frozen.parent / f"{review_id}.json", json.dumps(self.meta["review"], indent=2).encode())
            self._save()
            return preview + (f"\n[display truncated; full diff: {diff_path}]" if total_chars > 200_000 else "")

    def apply(self):
        """Only a user CLI action calls this; never exposed as an LLM tool."""
        with self.guard():
            review = self.meta.get("review")
            if not review:
                raise WorkspaceError("Review with /sandbox diff (or --sandbox-action diff) before apply")
            if self.scan() != review["tree"]:
                raise WorkspaceError("Copy changed since review; review the new diff first")
            changes = review["changes"]
            frozen = self.directory / "reviews" / review["id"]
            # Validate every candidate and conflict before making any original change.
            for name, entry in changes.items():
                target = checked_path(self.original, name)
                actual = fingerprint(*read_regular(target)) if target.exists() else None
                if actual != self.meta["baseline"].get(name):
                    raise WorkspaceError(f"Original changed; writeback conflict: {name}")
                if entry is not None and fingerprint(*read_regular(checked_path(frozen, name))) != entry:
                    raise WorkspaceError(f"Frozen candidate hash mismatch: {name}")
            backup = self.directory / "backups" / uuid.uuid4().hex
            backup.mkdir(parents=True)
            journal = {"status": "applying", "changes": changes, "completed": [], "recovery": []}
            log = backup / "journal.json"
            for name in changes:
                if name in self.meta["baseline"]:
                    data, mode = read_regular(checked_path(self.original, name))
                    if fingerprint(data, mode) != self.meta["baseline"][name]:
                        raise WorkspaceError(f"Original changed before backup: {name}")
                    atomic_write(backup / "files" / name, data, mode)
            atomic_write(log, json.dumps(journal, indent=2).encode())
            try:
                for name, entry in changes.items():
                    target = checked_path(self.original, name)
                    actual = fingerprint(*read_regular(target)) if target.exists() else None
                    if actual != self.meta["baseline"].get(name):
                        raise WorkspaceError(f"Original changed during apply: {name}")
                    if entry is None:
                        target.unlink()
                    else:
                        data, mode = read_regular(checked_path(frozen, name))
                        if fingerprint(data, mode) != entry:
                            raise WorkspaceError(f"Candidate changed during writeback: {name}")
                        atomic_write(target, data, mode)
                    journal["completed"].append(name)
                    atomic_write(log, json.dumps(journal, indent=2).encode())
            except BaseException:  # noqa: BLE001 -- rollback also on Ctrl+C/SystemExit
                for name in reversed(journal["completed"]):
                    try:
                        target = checked_path(self.original, name)
                        actual = fingerprint(*read_regular(target)) if target.exists() else None
                        if actual != changes[name]:
                            raise WorkspaceError("changed after write; manual recovery required")
                        if name in self.meta["baseline"]:
                            data, mode = read_regular(backup / "files" / name)
                            atomic_write(target, data, mode)
                        else:
                            target.unlink()
                        journal["recovery"].append(f"Restored {name}")
                    except Exception as e:  # noqa: BLE001 -- report each failed recovery independently
                        journal["recovery"].append(f"FAILED {name}: {e}")
                journal["status"] = "failed"
                log_error = ""
                try:
                    atomic_write(log, json.dumps(journal, indent=2).encode())
                except OSError as e:
                    log_error = f"; recovery journal could not be updated: {e}"
                raise WorkspaceError(f"Writeback failed; recovery results: {journal['recovery']}; journal: {log}{log_error}") from None
            journal["status"] = "applied"
            atomic_write(log, json.dumps(journal, indent=2).encode())
            self.meta["state"] = "applied"
            self._save()
            return f"Applied {len(changes)} file(s). Backups and journal: {backup}. Start a new session to continue."

    def discard(self):
        with self.guard():
            # Validate the resolved deletion target before recursive removal.
            if self.tree.resolve() != self.directory.resolve() / "tree" or _linked(self.tree.lstat()):
                raise WorkspaceError("Invalid copy deletion target")
            shutil.rmtree(self.tree)
            self.meta["state"] = "discarded"
            self._save()
            return "Discarded project copy; metadata and audit records retained."

    def checkpoint(self, path):
        name = path.relative_to(self.tree).as_posix()
        data, mode = read_regular(self.path(name)) if path.exists() else (None, 0o644)
        entries = self.meta.setdefault("checkpoints", [])
        entries.append({"path": name, "mode": mode, "prior": base64.b64encode(data).decode() if data is not None else None})
        self._save()

    def undo(self):
        with self.guard():
            entries = self.meta.get("checkpoints", [])
            if not entries:
                return "Nothing to undo in this sandbox. Bash changes are reviewed via /sandbox diff."
            entry = entries[-1]
            p = self.path(entry["path"])
            if entry["prior"] is None:
                p.unlink(missing_ok=True)
            else:
                atomic_write(p, base64.b64decode(entry["prior"]), entry.get("mode", 0o644))
            entries.pop()
            self._save()
            return f"Restored /workspace/{entry['path']}"


def tool_path(value):
    ws = CURRENT_WORKSPACE.get()
    return ws.path(value) if ws else Path(value).expanduser().resolve()


def display_path(path):
    ws = CURRENT_WORKSPACE.get()
    return ws.virtual(path) if ws else str(path)
