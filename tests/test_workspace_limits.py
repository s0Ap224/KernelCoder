"""Project boundary and managed-file budget checks; no remote service calls."""

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from kernelcoder import workspace as workspace_module
from kernelcoder import workspace_sync
from kernelcoder.workspace import MAX_FILE, MAX_TREE, SandboxWorkspace, WorkspaceError


@pytest.mark.parametrize('root', ['C:/', 'D:/'] if os.name == 'nt' else ['/'])
def test_drive_root_is_rejected_before_scan_or_store_creation(tmp_path, monkeypatch, root):
    scan = Mock(side_effect=AssertionError('must not scan a drive root'))
    monkeypatch.setattr(SandboxWorkspace, 'scan', scan)
    store = tmp_path / 'store'
    with pytest.raises(WorkspaceError, match='cannot be a drive/filesystem root'):
        SandboxWorkspace.create(root, store)
    assert not store.exists()
    scan.assert_not_called()


@pytest.mark.parametrize('size, accepted', [(10, True), (11, False)])
def test_creation_checks_total_budget_before_copying_files(tmp_path, monkeypatch, size, accepted):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'a.txt').write_bytes(b'a' * 5)
    (project / 'b.txt').write_bytes(b'b' * (size - 5))
    monkeypatch.setattr(workspace_module, 'MAX_TREE', 10)
    store = tmp_path / 'store'
    if accepted:
        ws = SandboxWorkspace.create(project, store)
        assert sum(entry['size'] for entry in ws.meta['baseline'].values()) == size
        assert ws.scan() == ws.meta['baseline']
    else:
        with pytest.raises(WorkspaceError, match='size limit exceeded'):
            SandboxWorkspace.create(project, store)
        assert not list(store.glob('*/tree/**/*.txt'))
        assert not list(store.glob('*/baseline/**/*.txt'))
    assert (project / 'a.txt').read_bytes() == b'a' * 5


def test_excluded_files_do_not_consume_managed_budget(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'code.py').write_bytes(b'pass')
    (project / 'large.data').write_bytes(b'x' * 11)
    monkeypatch.setattr(workspace_module, 'MAX_TREE', 10)
    ws = SandboxWorkspace.create(project, tmp_path / 'store', excludes=['large.data'])
    assert set(ws.scan()) == {'code.py'}


def test_existing_tree_growth_is_rejected_before_upload(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    ws = SandboxWorkspace.create(project, tmp_path / 'store')
    (ws.tree / 'a.txt').write_bytes(b'a' * 6)
    (ws.tree / 'b.txt').write_bytes(b'b' * 5)
    monkeypatch.setattr(workspace_module, 'MAX_TREE', 10)
    sandbox = Mock()
    with pytest.raises(WorkspaceError, match='size limit exceeded'):
        workspace_sync.WorkspaceSync(ws, sandbox, 'job').upload('true', 10, '.')
    sandbox.files.write.assert_not_called()
    sandbox.commands.run.assert_not_called()


def test_remote_manifest_uses_256_mib_total_budget(tmp_path):
    assert MAX_TREE == 256 * 1024 * 1024
    project = tmp_path / 'project'
    project.mkdir()
    ws = SandboxWorkspace.create(project, tmp_path / 'store')
    sizes = [MAX_FILE] * 8
    manifest = {f'{i}.bin': {'size': size, 'sha256': 'a' * 64, 'mode': 0o644}
                for i, size in enumerate(sizes)}
    assert workspace_sync.validate_manifest(ws, manifest) == manifest
    manifest['overflow.bin'] = {'size': 1, 'sha256': 'a' * 64, 'mode': 0o644}
    with pytest.raises(WorkspaceError, match='Remote workspace exceeds size limit'):
        workspace_sync.validate_manifest(ws, manifest)


def test_upload_sends_same_budget_to_remote_supervisor(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'code.py').write_text('pass', encoding='utf-8')
    ws = SandboxWorkspace.create(project, tmp_path / 'store')
    sandbox = Mock()
    monkeypatch.setattr(workspace_sync, 'invoke', Mock())
    workspace_sync.WorkspaceSync(ws, sandbox, 'job').upload('true', 10, '.')
    request_call = sandbox.files.write.call_args_list[0]
    request = json.loads(request_call.args[1])
    assert Path(request_call.args[0]).name == 'request.json'
    assert request['max_tree'] == MAX_TREE == 256 * 1024 * 1024
