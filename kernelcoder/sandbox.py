"""E2B command lifecycle. No execution path falls back to a host shell."""

import json
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .workspace import WorkspaceError
from .workspace_sync import CONTROL, WorkspaceSync, invoke, read_remote, recover_sync


@dataclass
class ExecutionResult:
    status: str
    output: str = ""
    exit_code: int | None = None
    detail: str = ""
    log: str | None = None

    def render(self):
        return (f"[{self.status}; exit={self.exit_code}] {self.detail}\n"
                f"{self.output or '(no output)'}" + (f"\n[log: {self.log}]" if self.log else ""))


class BoundedOutput:
    def __init__(self):
        self.head = b""
        self.tail = b""
        self.total = 0

    def add(self, data):
        self.total += len(data)
        needed = 6000 - len(self.head)
        self.head += data[:needed]
        self.tail = (self.tail + data[needed:])[-3000:]

    def text(self):
        gap = f"\n... truncated ({self.total} bytes) ...\n".encode() if self.total > 9000 else b""
        return (self.head + gap + self.tail).decode("utf-8", "replace")


def classify(command):
    """Convenience only. Unknown syntax never grants execution privileges."""
    if re.search(r"[|;&<>`$()\n]", command):
        return "unknown"
    try:
        words = shlex.split(command)
    except ValueError:
        return "unknown"
    if not words:
        return "empty"
    program = words[0].rsplit("/", 1)[-1]
    if program in {"vi", "vim", "nvim", "nano", "emacs", "less", "more", "top", "htop", "ssh", "passwd", "sudo"}:
        return "interactive"
    if len(words) == 1 and program in {"python", "python3", "node", "irb", "ipython", "bash", "sh"}:
        return "interactive"
    return "noninteractive"


class SandboxRunner:
    """One remote environment per workspace; injectable SDK class for tests."""

    def __init__(self, *, sandbox_type=None, api_key=None):
        self._type = sandbox_type
        self._key = api_key
        self._sessions = {}

    def check(self):
        from .config import resolved_environment
        values, _ = resolved_environment()
        self._key = self._key or values.get('E2B_API_KEY')
        if not self._key:
            raise WorkspaceError('E2B_API_KEY (or E2B_KEY) is missing. No host fallback.')
        if self._type is None:
            try:
                from e2b import Sandbox
            except ImportError as e:
                raise WorkspaceError('Install the e2b dependency. No host fallback.') from e
            self._type = Sandbox
        return 'E2B SDK and credentials configured (connection not yet verified).'

    def _connect(self, sid):
        self.check()
        return self._type.connect(sid, api_key=self._key)

    def _ensure(self, ws, timeout):
        cached = self._sessions.get(ws.id)
        if cached is not None and cached.sandbox_id != ws.meta.get('remote_id'):
            self._sessions.pop(ws.id)
            cached = None  # Another guarded CLI invocation replaced this session.
        if cached is not None:
            if not cached.is_running():
                self._sessions.pop(ws.id)
                ws.meta.pop('remote_id', None)
                ws._save()
            else:
                cached.set_timeout(timeout + 300)
                return cached
        # An idle ID persisted by another CLI invocation must be retired first.
        if ws.meta.get('remote_id'):
            self._destroy(ws.meta['remote_id'])
            ws.meta.pop('remote_id', None)
            ws._save()
        sandbox = self._type.create(api_key=self._key, timeout=timeout + 300,
                                    metadata={'kernelcoder_workspace': ws.id}, allow_internet_access=True)
        try:
            ws.meta['remote_id'] = sandbox.sandbox_id
            ws._save()
            sandbox.commands.run(f'mkdir -p {CONTROL} && chmod 700 {CONTROL}', user='root', timeout=20)
            source = Path(__file__).with_name('_sandbox_remote.py').read_text(encoding='utf-8')
            sandbox.files.write(f'{CONTROL}/supervisor.py', source, user='root')
            invoke(sandbox, 'setup')
        except BaseException:
            sandbox.kill()
            raise
        self._sessions[ws.id] = sandbox
        return sandbox

    def _destroy(self, sid):
        self.check()
        # SDK kill returns False for a nonexistent sandbox; both mean stopped.
        self._type.kill(sid, api_key=self._key)

    @staticmethod
    def _detail(exc):
        # Provider errors can contain request headers/URLs; do not echo them.
        return str(exc) if isinstance(exc, WorkspaceError) else f'{type(exc).__name__}; no command was retried'

    @staticmethod
    def _cancel(sandbox, job):
        sandbox.files.write(f'{CONTROL}/{job}/cancel', 'cancel', user='root')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if sandbox.files.exists(f'{CONTROL}/{job}/result.json', user='root'):
                return
            time.sleep(.1)
        raise WorkspaceError('Cannot confirm remote cancellation; cleanup is required')

    def probe(self):
        """Explicit billable connectivity check, independent of an LLM."""
        self.check()
        sandbox = None
        try:
            sandbox = self._type.create(api_key=self._key, timeout=60)
            result = sandbox.commands.run('printf kernelcoder-e2b-ready', timeout=15)
            if result.exit_code != 0 or result.stdout != 'kernelcoder-e2b-ready':
                raise WorkspaceError('Unexpected E2B probe result')
        except Exception as e:  # noqa: BLE001 -- SDK boundary; never echo credentials
            raise WorkspaceError(f'E2B connection check failed: {self._detail(e)}') from None
        finally:
            if sandbox is not None:
                try:
                    sandbox.kill()
                except Exception as e:  # noqa: BLE001 -- cleanup failure must remain visible
                    raise WorkspaceError(f'Probe cleanup unconfirmed for {sandbox.sandbox_id}: {type(e).__name__}') from None
        return 'E2B connected: remote command passed; probe sandbox destroyed.'

    def run(self, workspace, command, timeout=120, cwd=None):
        if not isinstance(command, str) or not command.strip() or "\0" in command:
            return ExecutionResult("policy_denied", detail="Command must be a nonempty string without NUL")
        if type(timeout) is not int or not 1 <= timeout <= 600:
            return ExecutionResult("policy_denied", detail="timeout must be an integer between 1 and 600")
        if cwd is not None and not isinstance(cwd, str):
            return ExecutionResult("policy_denied", detail="cwd must be a string")
        if classify(command) == "interactive":
            return ExecutionResult("policy_denied", detail="Interactive command: provide a noninteractive invocation")
        try:
            with workspace.guard():
                try:
                    directory = workspace.path(cwd or '/workspace')
                    if not directory.is_dir():
                        raise WorkspaceError('cwd must name an existing project directory')
                except (WorkspaceError, OSError) as e:
                    return ExecutionResult('policy_denied', detail=str(e))
                self.check()
                sandbox = self._ensure(workspace, timeout)
                job = uuid.uuid4().hex
                active = {'backend': 'e2b',
                          'sandbox_id': sandbox.sandbox_id,
                          'job': job,
                          'phase': 'uploading'}
                workspace.meta['active'] = active
                workspace._save()
                sync = WorkspaceSync(workspace, sandbox, job)
                before = sync.upload(command, timeout, directory.relative_to(workspace.tree).as_posix())
                active.update(phase='executing', before=before)
                workspace._save()  # Persist intent before sending a possibly ambiguous request.
                try:
                    invoke(sandbox, 'run', job, timeout=timeout + 30)
                except KeyboardInterrupt:
                    self._cancel(sandbox, job)
                result = ExecutionResult(**json.loads(read_remote(sandbox, f'{CONTROL}/{job}/result.json', 128_000)))
                active['phase'] = 'syncing'
                workspace._save()
                sync.download(before)
                active['phase'] = 'committed'
                workspace._save()
                workspace.meta['active'] = None
                workspace._save()
                try:
                    invoke(sandbox, 'prune', job)
                except Exception:  # noqa: BLE001 -- private scratch is removed on close
                    result.detail += ' Private remote scratch retained until session close.'
                return result
        except KeyboardInterrupt:
            status = 'runtime_error' if workspace.meta.get('active') else 'cancelled'
            return ExecutionResult(status, detail='Interrupted; inspect session status and run cleanup before reuse.')
        except Exception as e:  # noqa: BLE001 -- convert SDK/filesystem failures to the tool contract
            active = workspace.meta.get('active')
            status = 'sync_error' if isinstance(active, dict) and active.get('phase') == 'syncing' else 'runtime_error'
            return ExecutionResult(status, detail=self._detail(e))

    def cleanup(self, workspace):
        with workspace.guard(allow_active=True, allow_closed=True):
            active = workspace.meta.get('active')
            if active and (not isinstance(active, dict) or active.get('backend') != 'e2b'):
                raise WorkspaceError('Legacy execution: cannot confirm cleanup; active marker retained.')
            sid = workspace.meta.get('remote_id') or (active or {}).get('sandbox_id')
            if not sid and not workspace.meta.get('sync_pending'):
                return 'No active execution recorded. No backend cleanup performed.'
            try:
                if workspace.meta.get('sync_pending'):
                    recover_sync(workspace)
                    if active:
                        active['phase'] = 'committed'
                    workspace._save()
                if active and active['phase'] in ('executing', 'syncing'):
                    sandbox = self._sessions.get(workspace.id)
                    if sandbox is None or sandbox.sandbox_id != sid:
                        sandbox = self._connect(sid)
                    try:
                        self._cancel(sandbox, active['job'])
                        invoke(sandbox, 'stop', active['job'])
                        active['phase'] = 'syncing'
                        workspace._save()
                        WorkspaceSync(workspace, sandbox, active['job']).download(active['before'])
                        active['phase'] = 'committed'
                        workspace._save()
                    except BaseException:
                        # Do not destroy a stopped environment if retrying a download
                        # can recover data. For unknown execution, terminate it.
                        if active['phase'] == 'executing':
                            self._destroy(sid)
                        raise
                if sid:
                    self._destroy(sid)
                self._sessions.pop(workspace.id, None)
                workspace.meta.pop('remote_id', None)
                workspace.meta['active'] = None
                workspace._save()
            except Exception as e:  # noqa: BLE001 -- retain recovery metadata on any failure
                raise WorkspaceError(f'Cleanup incomplete; session remains locked: {self._detail(e)}') from None
            return 'E2B stopped; file synchronization recovered; session unlocked.'

    def close(self, workspace):
        """Release an idle environment; preserve unresolved synchronization."""
        with workspace.guard(allow_active=True, allow_closed=True):
            if workspace.meta.get('active') or workspace.meta.get('sync_pending'):
                raise WorkspaceError('Unfinished execution/sync retained. Run /sandbox cleanup before reuse.')
            sid = workspace.meta.get('remote_id')
            if sid:
                try:
                    self._destroy(sid)
                except Exception as e:  # noqa: BLE001 -- do not drop an unconfirmed remote ID
                    raise WorkspaceError(f'E2B close unconfirmed: {self._detail(e)}') from None
                workspace.meta.pop('remote_id', None)
                workspace._save()
            self._sessions.pop(workspace.id, None)
            return 'E2B environment closed.'
