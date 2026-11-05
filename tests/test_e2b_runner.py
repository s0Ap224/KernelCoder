"""Deterministic SDK contract tests; real remote execution is tested separately."""

import io
import json
import os
from types import SimpleNamespace
from typing import ClassVar

import pytest

from kernelcoder.agent import Agent
from kernelcoder.demo import ScriptedLLM
from kernelcoder.llm import ToolCall
from kernelcoder.sandbox import SandboxRunner
from kernelcoder.tools import ALL_TOOLS
from kernelcoder.tools.bash import BashTool
from kernelcoder.workspace import SandboxWorkspace, WorkspaceError, fingerprint
from kernelcoder.workspace_sync import CONTROL, local_mode, validate_manifest


class Files:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.data = {}

    def write(self, path, data, **kwargs):
        self.data[path] = data.encode() if isinstance(data, str) else bytes(data)

    def read(self, path, **kwargs):
        return io.BytesIO(self.data[path])

    def exists(self, path, **kwargs):
        return path in self.data


class Commands:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    def run(self, command, **kwargs):
        sbx = self.sandbox
        if 'supervisor.py' not in command:
            return SimpleNamespace(exit_code=0, stdout='kernelcoder-e2b-ready')
        action, *jobs = command.split('supervisor.py ', 1)[1].split()
        if action == 'setup':
            return
        job = jobs[0]
        remote = f'{CONTROL}/{job}'
        request = json.loads(sbx.files.data[f'{remote}/request.json'])
        if action == 'push':
            desired = request['files']
            for path in list(sbx.files.data):
                if path.startswith('/workspace/') and path[11:] not in desired and '/.venv/' not in path:
                    del sbx.files.data[path]
                    sbx.modes.pop(path, None)
            for name, entry in desired.items():
                sbx.files.data[f'/workspace/{name}'] = sbx.files.data[f'{remote}/upload/{name}']
                sbx.modes[f'/workspace/{name}'] = entry['mode']
        elif action == 'run':
            sbx.requests.append(request)
            result = sbx.behavior(sbx, request)
            sbx.files.write(f'{remote}/result.json', json.dumps(result))
            if sbx.interrupt:
                raise KeyboardInterrupt
        elif action == 'snapshot':
            if sbx.fail_download:
                raise OSError('simulated disconnection')
            manifest = {p[11:]: fingerprint(data, sbx.modes.get(p, 0o644))
                        for p, data in sbx.files.data.items() if p.startswith('/workspace/') and '/.venv/' not in p}
            sbx.files.write(f'{remote}/manifest.json', json.dumps(manifest))


class FakeSandbox:
    instances: ClassVar[dict] = {}
    created = 0

    def __init__(self):
        self.sandbox_id = str(type(self).created)
        self.files = Files(self)
        self.commands = Commands(self)
        self.alive = True
        self.requests = []
        self.modes = {}
        self.fail_download = False
        self.interrupt = False
        self.behavior = lambda sbx, req: {'status': 'completed', 'exit_code': 0, 'output': 'ok'}

    @classmethod
    def create(cls, **kwargs):
        cls.created += 1
        obj = cls()
        cls.instances[obj.sandbox_id] = obj
        assert 'envs' not in kwargs
        return obj

    @classmethod
    def connect(cls, sid, **kwargs):
        return cls.instances[sid]

    def kill(self, **kwargs):
        obj = self if isinstance(self, FakeSandbox) else FakeSandbox.instances[self]
        obj.alive = False

    def is_running(self):
        return self.alive

    def set_timeout(self, timeout):
        self.timeout = timeout


@pytest.fixture
def rig(tmp_path):
    original = tmp_path / 'project'
    original.mkdir()
    (original / 'code.py').write_text('value = 1\n')
    (original / '.env').write_text('E2B_KEY=never-upload-this')
    ws = SandboxWorkspace.create(original, tmp_path / 'store')
    runner = SandboxRunner(sandbox_type=FakeSandbox, api_key='fake-key')
    return ws, runner


def test_agent_edits_remote_results_and_explicit_apply(rig):
    ws, runner = rig
    agent = Agent(ScriptedLLM([]), tools=[BashTool(ws, runner), *[type(t)() for t in ALL_TOOLS if t.name != 'bash']],
                  workspace=ws)
    def call(name, **args):
        return agent._exec_tool(ToolCall('id', name, args))
    call('write_file', file_path='code.py', content='value = 2\n')
    assert '[completed; exit=0]' in call('bash', command='true')
    remote = runner._sessions[ws.id]
    assert remote.files.data['/workspace/code.py'] == (ws.tree / 'code.py').read_bytes()
    assert all('never-upload-this' not in data.decode(errors='ignore') for data in remote.files.data.values())
    def mutate(sbx, request):
        sbx.files.write('/workspace/中文.bin', b'\x00\xff\x80')
        del sbx.files.data['/workspace/code.py']
        return {'status': 'completed', 'exit_code': 7, 'output': '[stderr]\nfailed after writing'}
    remote.behavior = mutate
    assert '[completed; exit=7]' in call('bash', command='make_changes')
    assert not (ws.tree / 'code.py').exists()
    assert (ws.tree / '中文.bin').read_bytes() == b'\x00\xff\x80'
    assert (ws.original / 'code.py').read_text() == 'value = 1\n'
    ws.review()
    ws.apply()
    runner.close(ws)  # Closing an applied session must still release E2B.
    assert not remote.alive
    assert not (ws.original / 'code.py').exists()


def test_reuse_local_delete_and_dependency_cache(rig):
    ws, runner = rig
    assert runner.run(ws, 'first').status == 'completed'
    remote = runner._sessions[ws.id]
    remote.files.write('/workspace/.venv/dependency', b'cached')
    (ws.tree / 'code.py').unlink()
    result = runner.run(ws, 'second')
    assert result.status == 'completed'
    assert runner._sessions[ws.id] is remote
    assert '/workspace/code.py' not in remote.files.data
    assert remote.files.data['/workspace/.venv/dependency'] == b'cached'
    runner.close(ws)


def test_stale_cached_sandbox_does_not_erase_another_sessions_id(rig):
    ws, runner = rig
    runner.run(ws, 'first')
    old = runner._sessions[ws.id]
    other = SandboxRunner(sandbox_type=FakeSandbox, api_key='fake-key')
    other.run(ws, 'second')
    replacement = other._sessions[ws.id]
    assert not old.alive
    assert runner.run(ws, 'third').status == 'completed'
    assert not replacement.alive  # Its recorded ID was retired, not silently lost.
    runner.close(ws)


@pytest.mark.skipif(os.name != 'nt', reason='Windows readonly replacement semantics')
def test_sync_can_replace_readonly_windows_file(rig):
    ws, runner = rig
    (ws.tree / 'code.py').chmod(0o444)
    runner.run(ws, 'first')
    remote = runner._sessions[ws.id]
    def mutate(sbx, request):
        sbx.files.write('/workspace/code.py', b'updated readonly file')
        sbx.modes['/workspace/code.py'] = 0o644
        return {'status': 'completed', 'exit_code': 0}
    remote.behavior = mutate
    result = runner.run(ws, 'second')
    assert result.status == 'completed', result.render()
    assert (ws.tree / 'code.py').read_bytes() == b'updated readonly file'
    runner.close(ws)


def test_sync_failure_stays_locked_and_cleanup_recovers(rig):
    ws, runner = rig
    runner.run(ws, 'first')
    remote = runner._sessions[ws.id]
    remote.fail_download = True
    assert runner.run(ws, 'second').status == 'sync_error'
    assert ws.meta['active']['phase'] == 'syncing'
    for action in (ws.review, ws.apply, ws.discard):
        with pytest.raises(WorkspaceError, match='Unclean'):
            action()
    with pytest.raises(WorkspaceError, match='Unfinished'):
        runner.close(ws)
    remote.fail_download = False
    runner.cleanup(ws)
    assert ws.meta['active'] is None
    assert not remote.alive


def test_local_commit_failure_recovers_from_journal(rig, monkeypatch):
    import kernelcoder.workspace_sync as sync
    ws, runner = rig
    runner.run(ws, 'first')
    remote = runner._sessions[ws.id]
    def mutate(sbx, request):
        sbx.files.write('/workspace/a.txt', b'new')
        sbx.files.write('/workspace/code.py', b'value = 3\n')
        return {'status': 'completed', 'exit_code': 0}
    remote.behavior = mutate
    real = sync.atomic_write
    def fail(path, data, mode=0o644):
        if path == ws.tree / 'code.py':
            raise OSError('disk failure')
        return real(path, data, mode)
    monkeypatch.setattr(sync, 'atomic_write', fail)
    assert runner.run(ws, 'mutate').status == 'sync_error'
    assert ws.meta['sync_pending']
    monkeypatch.setattr(sync, 'atomic_write', real)
    runner.cleanup(ws)
    assert (ws.tree / 'a.txt').read_bytes() == b'new'
    assert (ws.tree / 'code.py').read_bytes() == b'value = 3\n'
    assert not ws.meta.get('sync_pending')


def test_cancel_consumes_confirmed_result(rig):
    ws, runner = rig
    runner.run(ws, 'first')
    remote = runner._sessions[ws.id]
    remote.interrupt = True
    remote.behavior = lambda *args: {'status': 'cancelled', 'exit_code': -9}
    assert runner.run(ws, 'second').status == 'cancelled'
    assert ws.meta['active'] is None


def test_cleanup_failure_preserves_marker(rig, monkeypatch):
    ws, runner = rig
    runner.run(ws, 'first')
    remote = runner._sessions[ws.id]
    remote.behavior = lambda *args: (_ for _ in ()).throw(OSError('disconnected'))
    runner.run(ws, 'second')
    monkeypatch.setattr(runner, '_cancel', lambda *args: (_ for _ in ()).throw(WorkspaceError('unconfirmed')))
    with pytest.raises(WorkspaceError, match='remains locked'):
        runner.cleanup(ws)
    assert ws.meta['active']['phase'] == 'executing'
    assert not remote.alive


def test_local_edit_during_sync_is_not_overwritten(rig):
    ws, runner = rig
    runner.run(ws, 'first')
    remote = runner._sessions[ws.id]
    def race(sbx, request):
        (ws.tree / 'code.py').write_bytes(b'local manual edit')
        sbx.files.write('/workspace/code.py', b'remote edit')
        return {'status': 'completed', 'exit_code': 0}
    remote.behavior = race
    assert runner.run(ws, 'second').status == 'sync_error'
    assert (ws.tree / 'code.py').read_bytes() == b'local manual edit'
    with pytest.raises(WorkspaceError):
        runner.cleanup(ws)
    assert remote.alive
    assert ws.meta['active']


def test_crash_after_import_does_not_redownload_over_new_tree(rig, monkeypatch):
    import kernelcoder.workspace_sync as sync
    ws, runner = rig
    runner.run(ws, 'first')
    remote = runner._sessions[ws.id]
    def mutate(sbx, request):
        sbx.files.write('/workspace/code.py', b'imported')
        return {'status': 'completed', 'exit_code': 0}
    remote.behavior = mutate
    original = sync.WorkspaceSync.download
    def crash_after_commit(self, before):
        original(self, before)
        raise OSError('crash after metadata commit')
    monkeypatch.setattr(sync.WorkspaceSync, 'download', crash_after_commit)
    assert runner.run(ws, 'second').status == 'runtime_error'
    resumed = SandboxWorkspace.open(ws.id, ws.directory.parent)
    assert resumed.meta['active']['phase'] == 'committed'
    runner.cleanup(resumed)
    assert (resumed.tree / 'code.py').read_bytes() == b'imported'


def test_ambiguous_execution_never_retried_or_unlocked(rig):
    ws, runner = rig
    runner.run(ws, 'first')
    remote = runner._sessions[ws.id]
    def disconnected(*args):
        raise OSError('connection lost')
    remote.behavior = disconnected
    assert runner.run(ws, 'second').status == 'runtime_error'
    assert ws.meta['active']['phase'] == 'executing'
    assert runner.run(ws, 'third').status == 'runtime_error'
    assert len(remote.requests) == 2


@pytest.mark.parametrize('cwd', ['../outside', '/etc', 'does-not-exist'])
def test_cwd_rejected_before_remote_creation(rig, cwd):
    ws, runner = rig
    assert runner.run(ws, 'true', cwd=cwd).status == 'policy_denied'
    assert not runner._sessions


def test_env_alias_and_standard_precedence(monkeypatch):
    monkeypatch.setenv('E2B_KEY', 'alias')
    runner = SandboxRunner(sandbox_type=FakeSandbox)
    runner.check()
    assert runner._key == 'alias'
    monkeypatch.setenv('E2B_API_KEY', 'standard')
    runner = SandboxRunner(sandbox_type=FakeSandbox)
    runner.check()
    assert runner._key == 'standard'


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a\\b', 'a:b', 'NUL', '.env', 'a?', 'a/./b'])
def test_remote_manifest_refuses_unsafe_names(rig, name):
    ws, _ = rig
    with pytest.raises(WorkspaceError):
        validate_manifest(ws, {name: fingerprint(b'x', 0o644)})


def test_remote_manifest_and_permission_normalization(rig, monkeypatch):
    ws, _ = rig
    with pytest.raises(WorkspaceError):
        validate_manifest(ws, {'a': fingerprint(b'', 0o644), 'a/b': fingerprint(b'', 0o644)})
    monkeypatch.setattr('kernelcoder.workspace_sync.os.name', 'nt')
    with pytest.raises(WorkspaceError, match='case collision'):
        validate_manifest(ws, {'A/x': fingerprint(b'', 0o644), 'a/y': fingerprint(b'', 0o644)})
    assert local_mode(0o755) == 0o666
    assert local_mode(0o444) == 0o444


def test_probe_always_destroys_environment():
    runner = SandboxRunner(sandbox_type=FakeSandbox, api_key='fake')
    assert 'destroyed' in runner.probe()
    assert not FakeSandbox.instances[str(FakeSandbox.created)].alive
