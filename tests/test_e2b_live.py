"""Opt-in real E2B acceptance: pytest tests/test_e2b_live.py --run-e2b.

Uses only a tiny temporary project; no repository contents or host credentials
are uploaded. The configured E2B credential stays in the SDK client.
"""

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from kernelcoder.agent import Agent
from kernelcoder.demo import ScriptedLLM
from kernelcoder.llm import LLMResponse, ToolCall
from kernelcoder.sandbox import SandboxRunner
from kernelcoder.tools import ALL_TOOLS
from kernelcoder.tools.bash import BashTool
from kernelcoder.workspace import SandboxWorkspace


def test_remote_agent_roundtrip_and_cleanup(tmp_path, request, monkeypatch):
    if not request.config.getoption('--run-e2b'):
        pytest.skip('Use --run-e2b to create a real, billable sandbox')
    config = dotenv_values(Path(__file__).resolve().parents[1] / '.env')
    key = os.getenv('E2B_API_KEY') or os.getenv('E2B_KEY') or config.get('E2B_API_KEY') or config.get('E2B_KEY')
    if not key:
        pytest.fail('E2B credential is not configured')
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'old.txt').write_bytes(b'old')
    (project / '.env').write_text('SENTINEL=must-not-upload')
    ws = SandboxWorkspace.create(project, tmp_path / 'store')
    runner = SandboxRunner(api_key=key)
    tools = [BashTool(ws, runner), *[type(t)() for t in ALL_TOOLS if t.name != 'bash']]
    script = [
        LLMResponse(tool_calls=[ToolCall('write', 'write_file', {
            'file_path': 'hello.py', 'content': "from pathlib import Path\nprint('REMOTE_OK')\n"
            "Path('result.bin').write_bytes(bytes([0, 255, 128]))\nPath('old.txt').unlink()\n"})]),
        LLMResponse(tool_calls=[ToolCall('run', 'bash', {'command': 'python hello.py'})]),
        LLMResponse(tool_calls=[ToolCall('read', 'read_file', {'file_path': 'hello.py'})]),
        LLMResponse(content='done'),
    ]
    agent = Agent(ScriptedLLM(script), tools=tools, workspace=ws)
    sid = None
    try:
        agent.chat('Run the temporary project and read its source.')
        outputs = {m.get('tool_call_id'): m['content'] for m in agent.messages if m['role'] == 'tool'}
        assert '[completed; exit=0]' in outputs['run'], outputs['run']
        assert 'REMOTE_OK' in outputs['run']
        assert 'Path' in outputs['read']
        assert (ws.tree / 'result.bin').read_bytes() == b'\x00\xff\x80'
        assert not (ws.tree / 'old.txt').exists()
        assert (project / 'old.txt').exists()
        sid = ws.meta['remote_id']
        # Installation is explicit and must persist across subsequent calls.
        result = runner.run(ws, 'python -m pip install --user --ignore-installed six==1.17.0 -q', timeout=120)
        assert result.exit_code == 0 and result.status == 'completed', result.render()
        result = runner.run(ws, "python -c 'import six; print(six.__version__)'")
        assert result.status == 'completed' and '1.17.0' in result.output, result.render()
        assert ws.meta['remote_id'] == sid
        result = runner.run(ws, "printf failed >&2; printf changed > failure.txt; exit 7")
        assert result.status == 'completed' and result.exit_code == 7, result.render()
        assert 'failed' in result.output
        assert (ws.tree / 'failure.txt').read_bytes() == b'changed'
        result = runner.run(ws, "printf partial > partial.txt; sleep 10", timeout=1)
        assert result.status == 'timeout', result.render()
        assert (ws.tree / 'partial.txt').read_bytes() == b'partial'
        result = runner.run(ws, "(sleep 2; printf bad > escaped.txt) & printf foreground")
        assert result.status == 'completed', result.render()
        result = runner.run(ws, "sleep 3; test ! -e escaped.txt; test ! -e /workspace/.env")
        assert result.status == 'completed' and result.exit_code == 0, result.render()
        result = runner.run(ws, "python -c 'print(\"x\" * 2000000); print(\"TAIL\")'")
        assert result.status == 'completed' and 'TAIL' in result.output and len(result.output) < 12000
        # Interrupt the client while a real supervisor is running remotely.
        import kernelcoder.sandbox as module
        from kernelcoder.workspace_sync import CONTROL
        original_invoke = module.invoke
        def interrupt(sandbox, action, job='', timeout=60):
            if action == 'run':
                handle = sandbox.commands.run(
                    f'/usr/bin/python3 -I {CONTROL}/supervisor.py run {job}',
                    user='root', cwd='/', background=True, timeout=30)
                handle.disconnect()
                raise KeyboardInterrupt
            return original_invoke(sandbox, action, job, timeout)
        with monkeypatch.context() as patch:
            patch.setattr(module, 'invoke', interrupt)
            result = runner.run(ws, 'sleep 20')
        assert result.status == 'cancelled', result.render()
        assert ws.meta['active'] is None
        # A symlink in a returned project must lock sync, never read its target.
        result = runner.run(ws, 'ln -s /etc/passwd unsafe-link')
        assert result.status == 'sync_error', result.render()
        runner._sessions[ws.id].commands.run('rm /workspace/unsafe-link', user='root', timeout=10)
        runner.cleanup(ws)
        result = runner.run(ws, 'test -f result.bin')
        assert result.status == 'completed' and result.exit_code == 0, result.render()
        sid = ws.meta['remote_id']
        ws.review()
        ws.apply()
        assert (project / 'result.bin').read_bytes() == b'\x00\xff\x80'
    finally:
        if ws.meta.get('active'):
            runner.cleanup(ws)
        else:
            runner.close(ws)
    assert 'remote_id' not in ws.meta
    if sid:
        from e2b import Sandbox
        # A second idempotent kill returning False confirms it was already gone.
        assert Sandbox.kill(sid, api_key=key) is False
