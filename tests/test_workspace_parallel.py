"""Workspace read concurrency and serial barriers, without E2B or timing races."""

import concurrent.futures
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from kernelcoder.agent import Agent
from kernelcoder.demo import ScriptedLLM
from kernelcoder.llm import LLMResponse, ToolCall
from kernelcoder.permissions import Permission
from kernelcoder.sandbox import ExecutionResult
from kernelcoder.skills import SkillLoader, SkillTool
from kernelcoder.tools.agent import AgentTool
from kernelcoder.tools.bash import BashTool
from kernelcoder.tools.edit import EditFileTool
from kernelcoder.tools.glob_tool import GlobTool
from kernelcoder.tools.grep import GrepTool
from kernelcoder.tools.read import ReadFileTool
from kernelcoder.tools.todo import TodoWriteTool
from kernelcoder.tools.write import WriteFileTool
from kernelcoder.workspace import CURRENT_WORKSPACE, SandboxWorkspace


@pytest.fixture
def ws(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'note.txt').write_text('old', encoding='utf-8')
    return SandboxWorkspace.create(project, tmp_path / 'store')


def call(i, name='read_file', **arguments):
    return ToolCall(id=str(i), name=name, arguments=arguments or {'file_path': 'note.txt'})


def make_agent(ws, tools=None, **kwargs):
    kwargs.setdefault("permission", Permission(allow_all=True, project_root=ws.original))
    return Agent(ScriptedLLM([]), tools=tools or [ReadFileTool()], workspace=ws, **kwargs)


def test_all_audited_reads_overlap_with_workspace_context(ws, tmp_path, monkeypatch):
    folder = tmp_path / 'skills' / 'example'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text('# Example\nInstructions', encoding='utf-8')
    tools = [ReadFileTool(), GlobTool(), GrepTool(), SkillTool(SkillLoader([folder.parent]))]
    barrier = threading.Barrier(4, timeout=5)
    main_thread = threading.get_ident()

    def wrap(original):
        def execute(self, **kwargs):
            assert threading.get_ident() != main_thread
            assert CURRENT_WORKSPACE.get() is ws
            barrier.wait()
            return original(self, **kwargs)
        return execute

    for tool in tools:
        monkeypatch.setattr(type(tool), 'execute', wrap(type(tool).execute))
    results = make_agent(ws, tools)._exec_tools_parallel([
        call(1), call(2, 'glob', pattern='*.txt'),
        call(3, 'grep', pattern='old'), call(4, 'load_skill', skill='example'),
    ])
    assert 'old' in results[0]
    assert 'note.txt' in results[1]
    assert 'old' in results[2]
    assert 'Instructions' in results[3]
    assert CURRENT_WORKSPACE.get() is None
    assert (ws.original / 'note.txt').read_text() == 'old'


def test_read_pool_has_eight_workers_and_preserves_result_order(ws, monkeypatch):
    barrier = threading.Barrier(8, timeout=5)
    mutex = threading.Lock()
    active = peak = 0
    threads = set()

    def execute(self, file_path):
        nonlocal active, peak
        with mutex:
            active += 1
            peak = max(peak, active)
            threads.add(threading.get_ident())
        try:
            if int(file_path) < 8:
                barrier.wait()
            return file_path
        finally:
            with mutex:
                active -= 1

    monkeypatch.setattr(ReadFileTool, 'execute', execute)
    results = make_agent(ws)._exec_tools_parallel([call(i, file_path=str(i)) for i in range(20)])
    assert results == [str(i) for i in range(20)]
    assert peak == 8 and len(threads) == 8


def test_out_of_order_completion_keeps_tool_message_ids(ws, monkeypatch):
    second_finished = threading.Event()
    completion = []

    def execute(self, file_path):
        if file_path == 'first':
            assert second_finished.wait(5)
        completion.append(file_path)
        if file_path == 'second':
            second_finished.set()
        return file_path

    monkeypatch.setattr(ReadFileTool, 'execute', execute)
    agent = make_agent(ws)
    agent.llm = ScriptedLLM([
        LLMResponse(tool_calls=[call(1, file_path='first'), call(2, file_path='second')]),
        LLMResponse(content='done'),
    ])
    assert agent.chat('read') == 'done'
    assert completion == ['second', 'first']
    messages = [m for m in agent.messages if m['role'] == 'tool']
    assert [(m['tool_call_id'], m['content']) for m in messages] == [('1', 'first'), ('2', 'second')]


def test_write_edit_and_bash_are_barriers_between_parallel_reads(ws, monkeypatch):
    barrier = threading.Barrier(2, timeout=5)
    original = ReadFileTool.execute
    active = 0
    mutex = threading.Lock()
    main_thread = threading.get_ident()

    def read(self, **kwargs):
        nonlocal active
        with mutex:
            active += 1
        try:
            barrier.wait()
            return original(self, **kwargs)
        finally:
            with mutex:
                active -= 1

    def run(workspace, command, timeout, cwd):
        assert threading.get_ident() == main_thread
        assert active == 0 and workspace is ws
        workspace.path('note.txt').write_text('remote', encoding='utf-8')
        return ExecutionResult('completed', exit_code=0)

    monkeypatch.setattr(ReadFileTool, 'execute', read)
    agent = make_agent(ws, [ReadFileTool(), WriteFileTool(), EditFileTool(),
                            BashTool(ws, SimpleNamespace(run=run))])
    results = agent._exec_tools_parallel([
        call(1), call(2),
        call(3, 'write_file', file_path='note.txt', content='written'),
        call(4), call(5), call(6, 'bash', command='printf remote'),
        call(7), call(8),
        call(9, 'edit_file', file_path='note.txt', old_string='remote', new_string='edited'),
        call(10), call(11),
    ])
    for indexes, expected in [((0, 1), 'old'), ((3, 4), 'written'), ((6, 7), 'remote'), ((9, 10), 'edited')]:
        assert all(expected in results[i] for i in indexes)
    assert (ws.original / 'note.txt').read_text() == 'old'


def test_todo_is_consent_free_but_serial_and_custom_read_is_serial(ws):
    main_thread = threading.get_ident()
    seen = []

    class CustomRead(ReadFileTool):
        def execute(self, file_path):
            assert threading.get_ident() == main_thread
            seen.append(file_path)
            return file_path

    todo = TodoWriteTool()
    agent = make_agent(ws, [CustomRead(), todo], permission=Permission(ask=lambda *_: "once"))
    assert 'todo_write' in Permission.READ_ONLY
    assert 'todo_write' not in Permission.PARALLEL_READ_ONLY
    assert not agent._parallel_read(call(1))
    results = agent._exec_tools_parallel([
        call(1, file_path='first'),
        call(2, 'todo_write', tasks=[{'content': 'one', 'status': 'pending'}]),
        call(3, file_path='second'),
        call(4, 'todo_write', tasks=[{'content': 'two', 'status': 'done'}]),
    ])
    assert seen == ['first', 'second']
    assert 'one' in results[1] and 'two' in results[3]
    assert todo._tasks == [{'content': 'two', 'status': 'done'}]


def test_another_agent_cannot_write_until_read_batch_finishes(ws, monkeypatch):
    original = ReadFileTool.execute
    barrier = threading.Barrier(2, timeout=5)
    readers_ready = threading.Event()
    writer_checked = threading.Event()
    release = threading.Event()
    finished = []
    lock_observations = []

    def read(self, **kwargs):
        if barrier.wait() == 0:
            readers_ready.set()
        assert release.wait(5)
        result = original(self, **kwargs)
        finished.append(True)
        return result

    def write():
        assert readers_ready.wait(5)
        acquired = ws._lock.acquire(blocking=False)
        lock_observations.append(acquired)
        if acquired:
            ws._lock.release()
        writer_checked.set()
        release.set()
        result = make_agent(ws, [WriteFileTool()])._exec_tool(
            call(3, 'write_file', file_path='note.txt', content='writer'),
        )
        assert len(finished) == 2
        return result

    monkeypatch.setattr(ReadFileTool, 'execute', read)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write)
        try:
            results = make_agent(ws)._exec_tools_parallel([call(1), call(2)])
        finally:
            release.set()
        assert writer_checked.is_set()
        assert lock_observations == [False]
        assert all('old' in result for result in results)
        assert 'Error' not in writer.result(timeout=5)
    assert ws.path('note.txt').read_text() == 'writer'


def test_consent_and_notifications_stay_on_main_thread(ws, monkeypatch):
    main_thread = threading.get_ident()
    events = []
    original = ReadFileTool.execute

    class Policy(Permission):
        def authorize(self, tool, arguments, *, context=None):
            assert threading.get_ident() == main_thread
            events.append('permission')
            return 'denied' if arguments.get('file_path') == 'denied' else None

    def notify(name, arguments):
        assert threading.get_ident() == main_thread
        events.append('notify')

    def read(self, **kwargs):
        assert events == ['notify'] * 3 + ['permission'] * 3
        assert kwargs['file_path'] != 'denied'
        return original(self, **kwargs)

    monkeypatch.setattr(ReadFileTool, 'execute', read)
    results = make_agent(ws, permission=Policy())._exec_tools_parallel(
        [call(1), call(2, file_path='denied'), call(3)], notify,
    )
    assert 'old' in results[0] and results[1] == 'denied' and 'old' in results[2]


@pytest.mark.parametrize('state', ['active', 'sync_pending', 'closed'])
def test_unclean_and_closed_workspaces_refuse_whole_read_batch(ws, monkeypatch, state):
    if state == 'closed':
        ws.meta['state'] = 'applied'
    else:
        ws.meta[state] = 'unfinished'
    ws._save()

    def forbidden(self, **kwargs):
        pytest.fail('blocked workspace reached tool execution')

    monkeypatch.setattr(ReadFileTool, 'execute', forbidden)
    results = make_agent(ws)._exec_tools_parallel([call(1), call(2)])
    assert all('Error executing read_file' in result for result in results)
    assert ws._depth == 0
    assert CURRENT_WORKSPACE.get() is None


def test_another_process_lock_blocks_read_batch(ws):
    source = (
        'import sys\nfrom kernelcoder.workspace import SandboxWorkspace\n'
        'with SandboxWorkspace(sys.argv[1]).guard():\n'
        ' print("locked", flush=True)\n sys.stdin.readline()\n'
    )
    proc = subprocess.Popen([sys.executable, '-c', source, str(ws.directory)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == 'locked'
        results = make_agent(ws)._exec_tools_parallel([call(1), call(2)])
        assert all('busy in another process' in result for result in results)
    finally:
        proc.communicate('\n', timeout=10)
    assert 'old' in make_agent(ws)._exec_tool(call(3))


def test_bad_arguments_exceptions_and_escape_do_not_poison_batch(ws, monkeypatch, tmp_path):
    original = ReadFileTool.execute

    def read(self, file_path, offset=1, limit=2000):
        if file_path == 'boom':
            raise RuntimeError('test failure')
        return original(self, file_path, offset, limit)

    monkeypatch.setattr(ReadFileTool, 'execute', read)
    secret = tmp_path / 'outside.txt'
    secret.write_text('HOST SECRET', encoding='utf-8')
    agent = make_agent(ws)
    results = agent._exec_tools_parallel([
        call(1, invalid=True), call(2, file_path='boom'), call(3, file_path=str(secret)), call(4),
    ])
    assert 'bad arguments' in results[0]
    assert 'test failure' in results[1]
    assert 'Error' in results[2] and 'HOST SECRET' not in results[2]
    assert 'old' in results[3]
    assert 'old' in agent._exec_tool(call(5))
    assert CURRENT_WORKSPACE.get() is None


def test_plan_mode_keeps_reads_and_refuses_write(ws):
    agent = make_agent(ws, [ReadFileTool(), WriteFileTool()], permission=Permission(allow_all=True))
    agent.plan_mode = True
    results = agent._exec_tools_parallel([
        call(1), call(2), call(3, 'write_file', file_path='note.txt', content='changed'), call(4),
    ])
    assert 'Plan mode' in results[2]
    assert all('old' in results[i] for i in (0, 1, 3))


def test_subagent_read_batch_reenters_outer_guard_without_deadlock(ws, monkeypatch):
    original = ReadFileTool.execute
    barrier = threading.Barrier(2, timeout=5)

    def read(self, **kwargs):
        assert CURRENT_WORKSPACE.get() is ws
        assert ws._depth >= 2
        barrier.wait()
        return original(self, **kwargs)

    monkeypatch.setattr(ReadFileTool, 'execute', read)
    agent = make_agent(ws, [AgentTool(), ReadFileTool()])
    agent.llm = ScriptedLLM([
        LLMResponse(tool_calls=[call(1), call(2)]), LLMResponse(content='child done'),
    ])
    assert 'child done' in agent._exec_tool(call(0, 'agent', task='read twice'))
    assert ws._depth == 0 and CURRENT_WORKSPACE.get() is None


def test_interrupt_cancels_queue_drains_readers_and_backfills(ws, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    executed = []
    real_pool = concurrent.futures.ThreadPoolExecutor
    submitted = []

    class OneWorkerPool(real_pool):
        def __init__(self, **kwargs):
            super().__init__(max_workers=1)

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            submitted.append(future)
            return future

    def read(self, file_path):
        executed.append(file_path)
        started.set()
        assert release.wait(5)
        assert ws._depth > 0
        finished.set()
        return 'done'

    def interrupt(futures):
        assert started.wait(5)
        raise KeyboardInterrupt

    real_wait = concurrent.futures.wait
    checks = []

    def check_drain(futures, **kwargs):
        assert ws._depth > 0 and CURRENT_WORKSPACE.get() is ws
        assert all(f.cancelled() for f in submitted[1:])
        checks.append(True)
        release.set()
        if len(checks) == 1:
            raise KeyboardInterrupt  # A repeated interrupt must still drain.
        return real_wait(futures, **kwargs)

    monkeypatch.setattr(ReadFileTool, 'execute', read)
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', OneWorkerPool)
    monkeypatch.setattr(concurrent.futures, 'as_completed', interrupt)
    monkeypatch.setattr(concurrent.futures, 'wait', check_drain)
    agent = make_agent(ws)
    agent.llm = ScriptedLLM([LLMResponse(tool_calls=[call(i, file_path=str(i)) for i in range(4)])])
    try:
        with pytest.raises(KeyboardInterrupt):
            agent.chat('read')
    finally:
        release.set()
    assert checks and finished.is_set() and executed == ['0']
    assert ws._depth == 0 and CURRENT_WORKSPACE.get() is None
    messages = [m for m in agent.messages if m['role'] == 'tool']
    assert [(m['tool_call_id'], m['content']) for m in messages] == [(str(i), '[interrupted]') for i in range(4)]
    with ws.guard():
        assert ws._depth == 1


def test_worker_interrupt_propagates_after_siblings_finish(ws, monkeypatch):
    barrier = threading.Barrier(2, timeout=5)
    finished = threading.Event()

    def read(self, file_path):
        barrier.wait()
        if file_path == 'interrupt':
            raise KeyboardInterrupt
        assert ws._depth > 0
        finished.set()
        return 'done'

    monkeypatch.setattr(ReadFileTool, 'execute', read)
    with pytest.raises(KeyboardInterrupt):
        make_agent(ws)._exec_tools_parallel([call(1, file_path='interrupt'), call(2)])
    assert finished.is_set() and ws._depth == 0
    assert CURRENT_WORKSPACE.get() is None
