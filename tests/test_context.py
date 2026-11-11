"""Behavioral regressions for recoverable, request-aware compaction."""
import copy
import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from kernelcoder import Agent
from kernelcoder.context import CompactResult, ContextLimitError, ContextManager, MessagePairingError, estimate_tokens
from kernelcoder.demo import ScriptedLLM
from kernelcoder.llm import LLMResponse, ToolCall
from kernelcoder.permissions import Permission as ScriptPermission
from kernelcoder.session import load_context_state, load_session, save_session
from kernelcoder.tools.base import Tool
from kernelcoder.tools.transcript import ReadTranscriptTool


def user(text):
    return {"role": "user", "content": text}


def batch(*contents, prefix="c"):
    calls = [{"id": f"{prefix}{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
             for i in range(len(contents))]
    return [{"role": "assistant", "content": None, "tool_calls": calls}] + [
        {"role": "tool", "tool_call_id": c["id"], "content": text} for c, text in zip(calls, contents)]


def result():
    return CompactResult(0, 0)


def records(ctx):
    return [json.loads(p.read_text(encoding="utf-8")) for p in ctx.store.directory.glob("*.json")]


class SummaryLLM:
    def __init__(self, response="Completed inspection; next run tests.", error=None):
        self.calls = []
        self.response, self.error = response, error

    def chat(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.response)


@pytest.mark.parametrize("length,changed", [(100000, False), (100001, True)])
def test_batch_threshold_and_recoverable_single_line(length, changed):
    ctx = ContextManager()
    original = "x" * length
    messages = batch(original)
    report = ctx.receive(messages)
    assert report.changed == changed
    if changed:
        meta = ctx._meta(messages[-1])
        assert messages[-1]["content"].startswith("x" * 2000 + "\n")
        saved = json.loads((ctx.store.directory / f"{meta['original']}.json").read_text())
        assert saved["content"] == original
        assert saved["stage"] == "tool_budget_constraint"
        assert saved["tool_call_id"] == "c0"
        count = len(records(ctx))
        ctx.receive(messages)
        assert len(records(ctx)) == count


def test_single_result_threshold_is_strict_and_batch_is_trigger_only():
    ctx = ContextManager()
    messages = batch(*(["a" * 20000] * 6))
    assert not ctx.receive(messages).changed
    assert sum(len(m["content"]) for m in messages[1:]) == 120000
    messages[-1]["content"] += "a"
    ctx.receive(messages)
    assert len(messages[-1]["content"]) < 2200
    assert all(len(m["content"]) == 20000 for m in messages[1:-1])


def test_budget_does_not_truncate_user_and_pending_batch_is_not_snipped():
    ctx = ContextManager()
    messages = [user("request" * 20000)] + [user(str(i)) for i in range(40)]
    pending = batch("a" * 100001, "b")
    messages.extend(pending[:-1])
    ctx.receive(messages)
    assert messages[0]["content"] == "request" * 20000
    assert len(messages) == 43
    assert len(messages[-1]["content"]) < 2200
    messages.append(pending[-1])
    ctx.receive(messages)
    assert len(messages) < 43
    ctx.units(messages)


def test_snip_samples_and_preserves_last_fifteen():
    ctx = ContextManager()
    messages = [user(str(i)) for i in range(1, 41)]
    assert not ctx.receive(messages).changed
    messages.append(user("41"))
    report = ctx.receive(messages)
    assert report.stages == ["snip_compact"]
    assert len(messages) == 20
    assert [m["content"] for m in messages if not m["content"].startswith("[snip")] == [
        "1", "2", "11", "20", *map(str, range(27, 42))]
    saved = next(r for r in records(ctx) if r["stage"] == "snip_compact" and isinstance(r["content"], list))
    assert {m["content"] for m in saved["content"]} == set(map(str, range(3, 27))) - {"11", "20"}
    event = next(r for r in records(ctx) if "before_messages" in r)
    assert event["before_messages"] == 41
    assert event["after_messages"] == 20
    assert event["content"]["snapshot"] == saved["artifact_id"]


def test_snip_expands_sampled_batch_and_preserves_request():
    ctx = ContextManager()
    messages = [user("first"), *batch(*(["result"] * 22)), *[user(str(i)) for i in range(25)]]
    active = messages[25]
    ctx.begin_turn(active)
    ctx.mark_seen(messages)
    ctx.receive(messages)
    assert active in messages
    assert len(messages) > 20
    ctx.units(messages)


def test_no_repeated_archive_when_everything_is_protected():
    ctx = ContextManager()
    messages = [user("first"), *batch(*(["result"] * 45))]
    assert not ctx.receive(messages).changed
    assert not ctx.receive(messages).changed
    assert records(ctx) == []


@pytest.mark.parametrize("messages", [
    [{"role": "tool", "tool_call_id": "unknown", "content": "bad"}],
    batch("a", "b")[:-1],
    batch("a") + [{"role": "tool", "tool_call_id": "c0", "content": "duplicate"}],
    batch("a")[:1] + [user("interleaved")],
])
def test_request_rejects_malformed_pairs(messages):
    with pytest.raises(MessagePairingError):
        ContextManager().prepare(messages, output_tokens=0)


def test_micro_uses_original_prefix_and_keeps_latest_five_and_unread():
    ctx = ContextManager()
    messages = []
    for i in range(9):
        messages.extend(batch(str(i) * 1000, prefix=f"c{i}"))
    ctx.mark_seen(messages)
    ctx._meta(messages[3])["seen"] = False  # An older unread batch must also survive.
    report = result()
    ctx.micro_compact(messages, 0, report)
    for i in (1, 5, 7):
        assert "micro_compact" in messages[i]["content"]
        assert len(messages[i]["content"].split("\n")[0]) == 150
    assert messages[3]["content"] == "1" * 1000
    assert all(len(messages[i]["content"]) == 1000 for i in range(9, 18, 2))
    assert report.stages == ["micro_compact"]


def test_micro_stops_at_target():
    ctx = ContextManager()
    messages = []
    for i in range(8):
        messages.extend(batch("x" * 1000, prefix=f"c{i}"))
    ctx.mark_seen(messages)
    target = estimate_tokens(messages) - 100
    ctx.micro_compact(messages, target, result())
    assert "micro_compact" in messages[1]["content"]
    assert messages[3]["content"] == "x" * 1000
    assert estimate_tokens(messages) <= target


def test_cross_stage_reference_still_points_to_full_original():
    ctx = ContextManager()
    messages = batch("x" * 100001)
    ctx.receive(messages)
    original_id = ctx._meta(messages[1])["original"]
    ctx._preview(messages[1], 150, "micro_compact", result())
    assert ctx._meta(messages[1])["original"] == original_id
    assert len([r for r in records(ctx) if r["content"] == "x" * 100001]) == 1
    assert {r["stage"] for r in records(ctx)} == {"tool_budget_constraint", "micro_compact"}


def test_fit_preserves_unread_batch_and_avoids_summary_call():
    ctx = ContextManager(max_tokens=5000)
    active = user("inspect output")
    ctx.begin_turn(active)
    messages = [active, *batch("x" * 18000)]
    llm = SummaryLLM()
    report = ctx.prepare(messages, llm, output_tokens=0)
    assert "fit_tool_results" in report.stages
    assert llm.calls == []
    assert active in messages
    assert not ctx._meta(messages[-1])["seen"]
    ctx.units(messages)


def test_summary_excludes_active_request_and_unread_results():
    ctx = ContextManager(max_tokens=6000)
    active = user("CURRENT_REQUEST_ONLY")
    ctx.begin_turn(active)
    messages = [user("old details " * 4000), active, *batch("UNREAD_RESULT_ONLY")]
    llm = SummaryLLM()
    report = ctx.prepare(messages, llm, output_tokens=0)
    assert "compact_history" in report.stages
    assert len(llm.calls) == 1
    sent = llm.calls[0]
    assert sent["max_tokens"] == 1500
    assert "CURRENT_REQUEST_ONLY" not in sent["messages"][1]["content"]
    assert "UNREAD_RESULT_ONLY" not in sent["messages"][1]["content"]
    assert estimate_tokens(sent["messages"]) + sent["max_tokens"] <= ctx.max_tokens * .95
    assert messages[1] is active
    assert messages[-1]["content"] == "UNREAD_RESULT_ONLY"
    assert sum(m.get("content") == "CURRENT_REQUEST_ONLY" for m in messages) == 1
    ctx.units(messages)


@pytest.mark.parametrize("llm", [SummaryLLM(""), SummaryLLM("x" * 60000), SummaryLLM(error=RuntimeError("offline"))])
def test_summary_failure_does_not_replace_history(llm):
    ctx = ContextManager(max_tokens=6000)
    messages = [user("older content " * 4000)]
    before = copy.deepcopy(messages)
    with pytest.raises(ContextLimitError):
        ctx.prepare(messages, llm, output_tokens=0)
    assert messages == before
    assert ctx.last_result.error


def test_large_active_request_cannot_be_hidden_from_budget():
    ctx = ContextManager(max_tokens=1000)
    active = user("原始指令必须保留" * 1000)
    ctx.begin_turn(active)
    messages = [active]
    llm = SummaryLLM()
    with pytest.raises(ContextLimitError, match="Protected"):
        ctx.prepare(messages, llm, output_tokens=0)
    assert messages == [active]
    assert not llm.calls


def test_system_tools_output_reservation_reduce_budget():
    ctx = ContextManager(max_tokens=10000)
    system = [{"role": "system", "content": "rules" * 100}]
    tools = [{"description": "long schema" * 100}]
    assert ctx.history_budget(system, tools, 2000) < ctx.history_budget([], [], 2000)
    assert ctx.history_budget([], [], 2000) == 7500


def test_archive_failure_prevents_mutation(monkeypatch):
    ctx = ContextManager()
    messages = batch("x" * 100001)
    before = copy.deepcopy(messages)
    monkeypatch.setattr(ctx.store, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        ctx.receive(messages)
    assert messages == before


def test_paged_reads_are_scoped_and_bounded():
    ctx = ContextManager()
    artifact = ctx.store.write("abcdefghij", "micro_compact")
    tool = ReadTranscriptTool(ctx)
    assert tool.execute(artifact, 3, 4).endswith("defg")
    assert tool.execute(artifact, 100, 4).endswith("\n")
    for bad in ("../outside", "/etc/passwd", "z" * 32):
        with pytest.raises(ValueError):
            tool.execute(bad)
    for offset, limit in ((-1, 1), (0, 10001), (0, 0)):
        with pytest.raises(ValueError):
            tool.execute(artifact, offset, limit)
    with pytest.raises(FileNotFoundError):
        ReadTranscriptTool(ContextManager()).execute(artifact)


def test_session_restore_and_reset_preserve_artifacts(tmp_path, monkeypatch):
    from kernelcoder import session
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path / "sessions")
    ctx = ContextManager()
    active = user("keep constraints")
    ctx.begin_turn(active)
    messages = [active, *batch("x" * 100001)]
    ctx.receive(messages)
    ctx.mark_seen(messages)
    artifact = ctx._meta(messages[-1])["original"]
    sid = save_session(messages, "test", context_state=ctx.export_state(messages))
    restored, _model = load_session(sid)
    other = ContextManager()
    other.restore_state(restored, load_context_state(sid))
    assert other.export_state(restored) == ctx.export_state(messages)
    assert other.store.read(artifact).endswith("x" * 2000)
    old_directory = other.store.directory
    other.reset()
    assert old_directory.exists()
    assert other.store.directory != old_directory
    with pytest.raises(FileNotFoundError):
        other.store.read(artifact)
    legacy = save_session([user("old format")], "test")
    other.restore_state(load_session(legacy)[0], load_context_state(legacy))
    assert other.active_id is not None


class CountTool(Tool):
    name, description, parameters = "count", "count executions", {"type": "object", "properties": {}}

    def __init__(self):
        self.count = 0

    def execute(self):
        self.count += 1
        return "executed"


class OverflowLLM:
    extra: ClassVar[dict] = {"max_tokens": 100}

    def __init__(self, persistent=False, unrelated=False):
        self.requests = 0
        self.persistent, self.unrelated = persistent, unrelated

    def chat(self, messages, **kwargs):
        if "max_tokens" in kwargs:  # Summary request, if old material exists.
            return LLMResponse(content="Earlier work completed.")
        self.requests += 1
        if self.requests == 1:
            return LLMResponse(tool_calls=[ToolCall("count1", "count", {})])
        if self.requests == 2 or self.persistent:
            raise ValueError("unrelated invalid parameter" if self.unrelated else "context_length_exceeded")
        return LLMResponse(content="done")


@pytest.mark.parametrize("persistent,unrelated,expected", [(False, False, 3), (True, False, 3), (False, True, 2)])
def test_overflow_retry_never_reexecutes_tools(persistent, unrelated, expected):
    tool = CountTool()
    llm = OverflowLLM(persistent, unrelated)
    agent = Agent(llm, tools=[tool], permission=ScriptPermission(ask=lambda *_: "once"))
    if persistent or unrelated:
        with pytest.raises(ValueError):
            agent.chat("work")
    else:
        assert agent.chat("work") == "done"
    assert llm.requests == expected
    assert tool.count == 1
    agent.context.units(agent.messages)


def test_child_archive_capability_is_rebound():
    parent = Agent(ScriptedLLM([]), tools=[], permission=ScriptPermission(ask=lambda *_: "once"))
    artifact = parent.context.store.write("private parent output", "compact_history")
    child = Agent(parent.llm, tools=parent.tools, conversation_id=parent.context.store.conversation_id, permission=ScriptPermission(ask=lambda *_: "once"))
    assert child.context.store.conversation_id == parent.context.store.conversation_id
    assert child.context.store.agent_id != parent.context.store.agent_id
    with pytest.raises(FileNotFoundError):
        child._tool_by_name["read_transcript"].execute(artifact)


def test_summary_previous_state_and_recent_tail_survive_input_fitting():
    ctx = ContextManager()
    summary = user("PREVIOUS_SUMMARY")
    ctx._meta(summary)["kind"] = "summary"
    messages = [summary, user("HEAD" + "x" * 30000 + "LATEST_DECISION")]
    text = ctx._summary_input(messages, 2000)
    assert "PREVIOUS_SUMMARY" in text
    assert "LATEST_DECISION" in text


def test_each_agent_step_preserves_pairing_and_only_success_marks_seen():
    tool = CountTool()
    agent = Agent(OverflowLLM(persistent=True), tools=[tool], permission=ScriptPermission(ask=lambda *_: "once"))
    with pytest.raises(ValueError, match="context_length_exceeded"):
        agent.chat("keep original")
    assert not agent.context._meta(agent.messages[-1])["seen"]
    assert agent.messages[0]["content"] == "keep original"


def test_completed_parallel_results_survive_archive_failure(monkeypatch):
    tool = CountTool()
    llm = ScriptedLLM([LLMResponse(tool_calls=[ToolCall("a", "count", {}), ToolCall("b", "count", {})])])
    agent = Agent(llm, tools=[tool], permission=ScriptPermission(ask=lambda *_: "once"))
    receive = agent.context.receive

    def fail_on_result(messages):
        if messages[-1].get("role") == "tool":
            raise OSError("archive unavailable")
        return receive(messages)

    monkeypatch.setattr(agent.context, "receive", fail_on_result)
    with pytest.raises(OSError):
        agent.chat("execute both")
    assert tool.count == 2
    assert [m["tool_call_id"] for m in agent.messages if m.get("role") == "tool"] == ["a", "b"]
    agent.context.units(agent.messages)


def test_manual_compact_summarizes_below_automatic_threshold():
    ctx = ContextManager()
    messages = [user("old task " * 200), {"role": "assistant", "content": "old answer " * 200}]
    llm = SummaryLLM()
    report = ctx.prepare(messages, llm, force=True)
    assert report.changed
    assert len(llm.calls) == 1
    assert llm.calls[0]["max_tokens"] == 2000
    assert len(messages) == 1


def test_large_tool_arguments_are_never_silently_trimmed():
    ctx = ContextManager(max_tokens=1000)
    messages = batch("tiny result")
    messages[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({"content": "x" * 15000})
    before = copy.deepcopy(messages)
    with pytest.raises(ContextLimitError):
        ctx.prepare(messages, SummaryLLM(), output_tokens=0)
    assert messages == before


def test_summary_call_override_does_not_change_shared_model_settings(monkeypatch):
    from kernelcoder.llm import LLM
    llm = object.__new__(LLM)
    llm.model = "offline"
    llm.extra = {"max_completion_tokens": 8192, "temperature": .4}
    requests = []

    def capture(params):
        requests.append(dict(params))
        return iter(())

    monkeypatch.setattr(llm, "_call_with_retry", capture)
    monkeypatch.setattr(llm, "_drain", lambda *args: LLMResponse(content="summary"))
    llm.chat([user("summarize")], max_tokens=2000)
    llm.chat([user("ordinary request")])
    assert requests[0]["max_tokens"] == 2000
    assert "max_completion_tokens" not in requests[0]
    assert requests[1]["max_completion_tokens"] == 8192
    assert llm.extra == {"max_completion_tokens": 8192, "temperature": .4}


def test_no_summary_wire_metadata_and_reset_rebinds_reader():
    agent = Agent(ScriptedLLM([LLMResponse(content="ok")]), tools=[], permission=ScriptPermission(ask=lambda *_: "once"))
    agent.chat("hello")
    artifact = agent.context.store.write("old namespace", "compact_history")
    assert all(set(m) == {"role", "content"} for m in agent.messages)
    agent.reset()
    with pytest.raises(FileNotFoundError):
        agent._tool_by_name["read_transcript"].execute(artifact)


def test_mixed_unicode_summary_input_fits_and_preserves_tail():
    ctx = ContextManager(max_tokens=6000)
    messages = [user("中文内容" * 12000 + "RECENT_FACT")]
    llm = SummaryLLM()
    ctx.prepare(messages, llm, output_tokens=0)
    sent = llm.calls[0]
    assert estimate_tokens(sent["messages"]) + sent["max_tokens"] <= 5700
    assert "RECENT_FACT" in sent["messages"][-1]["content"]
