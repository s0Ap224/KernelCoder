"""Validated, staged synchronization between a local copy and E2B.

The remote supervisor never prints project files. Transfers are bounded, and
local commits can be resumed from a durable journal after interruption.
"""

import hashlib
import json
import os
import re
import shlex
import uuid
from pathlib import PurePosixPath

from .workspace import (
    MAX_FILE,
    MAX_FILES,
    MAX_TREE,
    WorkspaceError,
    _parts,
    atomic_write,
    checked_path,
    fingerprint,
    read_regular,
)

CONTROL = '/root/.kernelcoder-runner'


def invoke(sandbox, action, job='', timeout=60):
    return sandbox.commands.run(
        f'/usr/bin/python3 -I {CONTROL}/supervisor.py {shlex.quote(action)} {shlex.quote(job)}',
        user='root', cwd='/', timeout=timeout,
    )


def read_remote(sandbox, path, limit):
    data = bytearray()
    stream = sandbox.files.read(path, format='stream', user='root', request_timeout=60)
    try:
        for chunk in stream:
            if len(data) + len(chunk) > limit:
                raise WorkspaceError('Remote transfer exceeds size limit')
            data.extend(chunk)
    finally:
        stream.close()
    return bytes(data)


def local_mode(mode):
    return (0o666 if mode & 0o200 else 0o444) if os.name == 'nt' else mode


def validate_manifest(ws, manifest):
    if not isinstance(manifest, dict) or len(manifest) > MAX_FILES:
        raise WorkspaceError('Invalid remote file manifest')
    total, paths = 0, {}
    for name, entry in manifest.items():
        if not isinstance(name, str) or '\\' in name or not name or '/'.join(_parts(name)) != name:
            raise WorkspaceError('Noncanonical remote path')
        if any(c in name for c in '<>"|?*') or ws.excluded(name):
            raise WorkspaceError('Excluded or unsupported remote path')
        # Reject case collisions in directories as well as filenames on Windows.
        for prefix in (PurePosixPath(name), *PurePosixPath(name).parents):
            key = str(prefix).casefold() if os.name == 'nt' else str(prefix)
            if key in paths and paths[key] != str(prefix):
                raise WorkspaceError('Remote path case collision')
            paths[key] = str(prefix)
        if not isinstance(entry, dict) or set(entry) != {'sha256', 'size', 'mode'}:
            raise WorkspaceError('Invalid remote file entry')
        if (type(entry['size']) is not int or not 0 <= entry['size'] <= MAX_FILE
                or type(entry['mode']) is not int or not 0 <= entry['mode'] <= 0o777
                or not isinstance(entry['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', entry['sha256'])):
            raise WorkspaceError('Invalid remote fingerprint')
        total += entry['size']
        if total > MAX_TREE:
            raise WorkspaceError('Remote workspace exceeds size limit')
    for name in manifest:
        if any(str(parent) in manifest for parent in PurePosixPath(name).parents):
            raise WorkspaceError('Remote file/directory collision')
    return manifest


class WorkspaceSync:
    def __init__(self, workspace, sandbox, job):
        self.ws, self.sandbox, self.job = workspace, sandbox, job
        self.remote = f'{CONTROL}/{job}'

    def upload(self, command, timeout, cwd):
        files = self.ws.scan()
        validate_manifest(self.ws, files)
        request = {'files': files, 'excludes': self.ws.meta['excludes'],
                   'max_file': MAX_FILE, 'max_tree': MAX_TREE, 'max_files': MAX_FILES,
                   'command': command, 'timeout': timeout, 'cwd': cwd}
        self.sandbox.files.write(f'{self.remote}/request.json', json.dumps(request), user='root')
        # Upload the managed copy only; no session metadata or host environment.
        for name, entry in files.items():
            data, mode = read_regular(self.ws.path(name))
            if fingerprint(data, mode) != entry:
                raise WorkspaceError('Local copy changed during upload')
            self.sandbox.files.write(f'{self.remote}/upload/{name}', data, user='root')
        invoke(self.sandbox, 'push', self.job)
        if self.ws.scan() != files:
            raise WorkspaceError('Local copy changed during upload')
        return files

    def download(self, before):
        invoke(self.sandbox, 'snapshot', self.job)
        manifest = validate_manifest(self.ws, json.loads(read_remote(
            self.sandbox, f'{self.remote}/manifest.json', MAX_FILES * 2048)))
        txid = uuid.uuid4().hex
        directory = self.ws.directory / 'sync' / txid
        (directory / 'candidate').mkdir(parents=True)
        (directory / 'backup').mkdir()
        desired = {}
        for name, entry in manifest.items():
            data = read_remote(self.sandbox, f'/workspace/{name}', entry['size'])
            if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise WorkspaceError('Remote file changed during download')
            mode = local_mode(entry['mode'])
            atomic_write(directory / 'candidate' / name, data, mode)
            desired[name] = fingerprint(data, mode)
        if self.ws.scan() != before:
            raise WorkspaceError('Local copy changed during remote execution')
        for name, entry in before.items():
            data, mode = read_regular(self.ws.path(name))
            if fingerprint(data, mode) != entry:
                raise WorkspaceError('Local copy changed before sync backup')
            atomic_write(directory / 'backup' / name, data, mode)
        writable = [n for n, e in before.items() if os.name == 'nt' and not e['mode'] & 0o200 and desired.get(n) != e]
        journal = {'state': 'prepared', 'before': before, 'desired': desired, 'writable': writable}
        atomic_write(directory / 'journal.json', json.dumps(journal).encode())
        self.ws.meta['sync_pending'] = txid
        self.ws._save()
        recover_sync(self.ws)


def recover_sync(ws):
    """Roll forward an interrupted local import, accepting only old/new hashes.

    Caller holds guard(allow_active=True). Remote cleanup never clears this
    journal; successful import is required before unlocking the workspace.
    """
    txid = ws.meta.get('sync_pending')
    if not txid:
        return
    if not re.fullmatch('[a-f0-9]{32}', txid):
        raise WorkspaceError('Invalid synchronization journal ID')
    directory = ws.directory / 'sync' / txid
    journal = json.loads((directory / 'journal.json').read_text(encoding='utf-8'))
    before = validate_manifest(ws, journal['before'])
    desired = validate_manifest(ws, journal['desired'])
    current = ws.scan()
    for name in before.keys() | desired.keys() | current.keys():
        allowed = [before.get(name), desired.get(name)]
        if name in journal.get('writable', []) and name in before:
            allowed.append({**before[name], 'mode': 0o666})
        if current.get(name) not in allowed:
            raise WorkspaceError(f'Local edit conflicts with sync recovery: {name}')
    # Verify all staged content before touching the tree.
    for name, entry in desired.items():
        if fingerprint(*read_regular(checked_path(directory / 'candidate', name))) != entry:
            raise WorkspaceError('Sync candidate hash mismatch')
    for name in sorted(before.keys() - desired.keys(), key=len, reverse=True):
        if name not in current:
            continue  # A prior interrupted commit may already have replaced the parent directory.
        p = ws.path(name)
        if p.is_file():
            if os.name == 'nt':
                p.chmod(0o666)
            p.unlink()
            parent = p.parent
            while parent != ws.tree:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    for name, entry in desired.items():
        p = ws.path(name)
        if p.is_dir():
            p.rmdir()
        actual = fingerprint(*read_regular(p)) if p.exists() else None
        if actual != entry:
            data, mode = read_regular(checked_path(directory / 'candidate', name))
            if os.name == 'nt' and p.exists():
                p.chmod(0o666)
            atomic_write(p, data, mode)
    if ws.scan() != desired:
        raise WorkspaceError('Local synchronization verification failed')
    journal['state'] = 'committed'
    atomic_write(directory / 'journal.json', json.dumps(journal).encode())
    ws.meta.pop('sync_pending', None)
    active = ws.meta.get('active')
    if isinstance(active, dict) and active.get('backend') == 'e2b':
        # Commit the phase and remove the journal pointer in ONE metadata write.
        # Otherwise a crash here could make cleanup download over an imported tree.
        active['phase'] = 'committed'
    ws._save()
