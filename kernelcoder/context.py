"""Four-stage context compaction with recoverable artifacts and protected inputs."""
from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field

from .prompt import COMPACT_PROMPT
from .transcripts import TranscriptStore, checked_id

_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿　-〿＀-･]")
_SYMBOL_RE = re.compile(r"[^\w\s]")


def _approx_tokens(text: str) -> int:
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    dense = rest > 0 and len(_SYMBOL_RE.findall(text)) / len(text) > 0.25
    return math.ceil(cjk / 1.5) + math.ceil(rest / (2.8 if dense else 3.4))


def estimate_tokens(messages: list[dict]) -> int:
    # Include message framing, IDs, and serialized arguments, not just prose.
    return sum(4 + _approx_tokens(json.dumps(m, ensure_ascii=False)) for m in messages)


class ContextLimitError(ValueError):
    """Protected input cannot fit; the original remains available."""


class MessagePairingError(ValueError):
    """A tool batch is malformed or incomplete at the request boundary."""


def is_context_overflow(error):
    code = str(getattr(error, "code", "") or "").lower()
    text = str(error).lower()
    return code in {"context_length_exceeded", "prompt_too_long"} or any(
        marker in text for marker in (
            "context_length_exceeded", "prompt_too_long", "prompt is too long",
            "maximum context length", "context window exceeded", "too many tokens",
        )
    )


@dataclass
class CompactResult:
    before_tokens: int
    after_tokens: int
    stages: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def changed(self):
        return bool(self.stages)


class ContextManager:
    BATCH_LIMIT = 100_000
    RESULT_LIMIT = 20_000
    PREVIEW = 2000
    MESSAGE_LIMIT = 40
    KEEP_RESULTS = 5

    def __init__(self, max_tokens=128_000, *, conversation_id=None):
        self.max_tokens = max_tokens
        self.store = TranscriptStore(conversation_id=conversation_id)
        self._records = {}
        self.active_id = None
        self._last_snip = None
        self.last_result = CompactResult(0, 0)

    def _meta(self, message):
        key = id(message)
        if key not in self._records:
            self._records[key] = (message, {"id": uuid.uuid4().hex, "seen": False})
        return self._records[key][1]

    def _sync(self, messages):
        self._records = {id(m): (m, self._meta(m)) for m in messages}

    def begin_turn(self, message):
        self.active_id = self._meta(message)["id"]

    def mark_seen(self, messages):
        for m in messages:
            if m.get("role") == "tool":
                self._meta(m)["seen"] = True

    @staticmethod
    def units(messages, *, allow_pending=False):
        """Return indivisible index ranges; only a trailing batch may be pending."""
        units, i = [], 0
        call_ids = set()
        closed = True
        while i < len(messages):
            start = i
            m = messages[i]
            if m.get("role") == "tool":
                raise MessagePairingError("Tool result has no preceding assistant call")
            calls = m.get("tool_calls") or []
            i += 1
            if calls:
                ids = [c.get("id") for c in calls]
                if m.get("role") != "assistant" or not all(ids) or len(set(ids)) != len(ids):
                    raise MessagePairingError("Invalid assistant tool call IDs")
                if call_ids.intersection(ids):
                    raise MessagePairingError("Duplicate tool call ID")
                call_ids.update(ids)
                remaining = set(ids)
                while i < len(messages) and messages[i].get("role") == "tool":
                    result_id = messages[i].get("tool_call_id")
                    if result_id not in remaining:
                        raise MessagePairingError("Unknown or duplicate tool result ID")
                    remaining.remove(result_id)
                    i += 1
                if remaining:
                    if not allow_pending or i != len(messages):
                        raise MessagePairingError("Assistant tool calls have missing replies")
                    closed = False
            units.append(list(range(start, i)))
        return units, closed

    def _protected(self, messages):
        protected = {i for i, m in enumerate(messages) if self._meta(m)["id"] == self.active_id}
        units, _ = self.units(messages)
        for unit in units:
            if any(messages[i].get("role") == "tool" and not self._meta(messages[i])["seen"] for i in unit):
                protected.update(unit)
        return protected

    def _record(self, result, stage, artifact):
        if stage not in result.stages:
            result.stages.append(stage)
        result.artifacts.append(artifact)

    def _snapshot(self, messages, stage):
        return self.store.write(messages, stage, message_ids=[self._meta(m)["id"] for m in messages],
                                before_tokens=estimate_tokens(messages))

    def _preview(self, message, chars, stage, result):
        text = message.get("content") or ""
        meta = self._meta(message)
        # Preview always derives from the original prefix, never from a prior marker.
        prefix = meta.get("prefix", text[:self.PREVIEW])[:chars]
        ref = meta.get("original", "0" * 32)
        replacement = f"{prefix}\n[Saved tool result: {ref}; use read_transcript; stage={stage}]"
        if len(replacement) >= len(text):
            return False
        original = meta.get("original")
        if not original:
            original = self.store.write(text, stage, message_id=meta["id"],
                                        tool_call_id=message.get("tool_call_id"), before_chars=len(text))
        replacement = f"{prefix}\n[Saved tool result: {original}; use read_transcript; stage={stage}]"
        event = self.store.write({"original": original}, stage, message_id=meta["id"],
                                 tool_call_id=message.get("tool_call_id"),
                                 before_chars=len(text), after_chars=len(replacement))
        meta.update(original=original, prefix=meta.get("prefix", text[:self.PREVIEW]))
        message["content"] = replacement
        self._record(result, stage, original)
        result.artifacts.append(event)
        return True

    def tool_budget_constraint(self, messages, result):
        units, _ = self.units(messages, allow_pending=True)
        if not units or not messages[units[-1][0]].get("tool_calls"):
            return
        batch = [messages[i] for i in units[-1] if messages[i].get("role") == "tool"]
        total = sum(len(m.get("content") or "") for m in batch)
        for m in sorted(batch, key=lambda m: len(m.get("content") or ""), reverse=True):
            if total <= self.BATCH_LIMIT:
                break
            before = len(m.get("content") or "")
            if before > self.RESULT_LIMIT:
                self._preview(m, self.PREVIEW, "tool_budget_constraint", result)
                total -= before - len(m["content"])

    def snip_compact(self, messages, result):
        if len(messages) <= self.MESSAGE_LIMIT:
            return
        signature = tuple(self._meta(m)["id"] for m in messages)
        if signature == self._last_snip:
            return
        units, _ = self.units(messages)
        keep = {0, 1, 10, 19, *range(max(0, len(messages) - 15), len(messages))}
        keep.update(self._protected(messages))
        for unit in units:
            if keep.intersection(unit):
                keep.update(unit)
        omitted = [m for i, m in enumerate(messages) if i not in keep]
        if len(omitted) <= 1 or all(self._meta(m).get("kind") == "archive" for m in omitted):
            self._last_snip = signature
            return
        artifact = self._snapshot(omitted, "snip_compact")
        marker = {"role": "user", "content":
                  f"[snip_compact: {len(omitted)} messages archived; read_transcript artifact_id={artifact}]"}
        self._meta(marker)["kind"] = "archive"
        # Insert at the first omitted whole unit, never between calls and replies.
        insertion = next(i for i in range(len(messages)) if i not in keep)
        candidate = []
        for i, m in enumerate(messages):
            if i == insertion:
                candidate.append(marker)
            if i in keep:
                candidate.append(m)
        event = self.store.write({"snapshot": artifact}, "snip_compact",
                                 before_tokens=estimate_tokens(messages), after_tokens=estimate_tokens(candidate),
                                 before_messages=len(messages), after_messages=len(candidate))
        messages[:] = candidate
        self._last_snip = tuple(self._meta(m)["id"] for m in messages)
        self._record(result, "snip_compact", artifact)
        result.artifacts.append(event)

    def receive(self, messages):
        result = CompactResult(estimate_tokens(messages), 0)
        self.tool_budget_constraint(messages, result)
        _, closed = self.units(messages, allow_pending=True)
        if closed:
            self.snip_compact(messages, result)
        return self._finish(messages, result)

    def _finish(self, messages, result):
        result.after_tokens = estimate_tokens(messages)
        self._sync(messages)
        self.last_result = result
        return result

    def history_budget(self, system, tools, output_tokens):
        overhead = estimate_tokens(system)
        if tools:
            overhead += _approx_tokens(json.dumps(tools, ensure_ascii=False))
        return self.max_tokens - overhead - output_tokens - math.ceil(self.max_tokens * .05)

    def micro_compact(self, messages, target, result):
        tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        protected = self._protected(messages) | set(tool_indexes[-self.KEEP_RESULTS:])
        for i in tool_indexes:
            if estimate_tokens(messages) <= target:
                break
            if i not in protected:
                self._preview(messages[i], 150, "micro_compact", result)

    def _summary_input(self, messages, budget):
        """Bound summary material, retaining prior summaries and history head/tail."""
        text = json.dumps(messages, ensure_ascii=False)
        if _approx_tokens(text) <= budget:
            return text
        previous = json.dumps([m for m in messages if self._meta(m).get("kind") == "summary"], ensure_ascii=False)
        # Budget must include separators. Binary-search a character allowance using
        # the same estimator as the request path, including CJK-heavy inputs.
        def select(n):
            return (previous[:n // 4] + "\n[earlier material; omissions archived]\n" + text[:n // 4]
                    + "\n[recent material]\n" + (text[-(n // 2):] if n // 2 else ""))
        low, high = 0, len(text) + len(previous)
        while low < high:
            middle = (low + high + 1) // 2
            if _approx_tokens(select(middle)) <= budget:
                low = middle
            else:
                high = middle - 1
        return select(low) if _approx_tokens(select(low)) <= budget else ""

    def compact_history(self, messages, llm, target, result, *, force_summary=False):
        protected = self._protected(messages)
        old = [m for i, m in enumerate(messages) if i not in protected]
        # Manual compact on empty/trivial history is a no-op.
        if force_summary and len(old) <= 1 and estimate_tokens(messages) <= target:
            return
        artifact = self._snapshot(messages, "compact_history")
        result.artifacts.append(artifact)
        for m in sorted((m for m in messages if m.get("role") == "tool"),
                        key=lambda m: len(m.get("content") or ""), reverse=True):
            if estimate_tokens(messages) <= target:
                break
            self._preview(m, self.PREVIEW, "fit_tool_results", result)
        # If 2,000-character previews still do not fit, shrink to 150, then refs.
        for chars in (150, 0):
            for m in messages:
                if estimate_tokens(messages) <= target:
                    break
                if m.get("role") == "tool":
                    self._preview(m, chars, "fit_tool_results", result)
        if estimate_tokens(messages) <= target and not force_summary:
            return
        old = [m for i, m in enumerate(messages) if i not in protected]
        if not old or llm is None:
            result.error = "No summarizable history or summary model available"
            return
        summary_system = [{"role": "system", "content": COMPACT_PROMPT}]
        output_limit = min(2000, max(1, self.max_tokens // 4))
        input_budget = self.history_budget(summary_system, [], output_limit) - 64
        text = self._summary_input(old, input_budget) if input_budget > 0 else ""
        if not text:
            result.error = "No room for summary input"
            return
        try:
            response = llm.chat(messages=summary_system + [{"role": "user", "content": text}],
                                max_tokens=output_limit)
            summary = (response.content or "").strip()
        except Exception as error:  # noqa: BLE001 -- provider failures must not erase history
            result.error = f"Summary failed: {error}"
            return
        if not summary:
            result.error = "Summary was empty"
            return
        message = {"role": "user", "content":
                   f"[Conversation summary; reference only; transcript={artifact}]\n{summary}"}
        candidate = [message] + [m for i, m in enumerate(messages) if i in protected]
        self.units(candidate)
        if estimate_tokens(candidate) >= estimate_tokens(messages):
            result.error = "Summary did not reduce context"
            return
        event = self.store.write({"snapshot": artifact}, "compact_history",
                                 before_tokens=estimate_tokens(messages), after_tokens=estimate_tokens(candidate))
        self._meta(message)["kind"] = "summary"
        messages[:] = candidate
        self._record(result, "compact_history", artifact)
        result.artifacts.append(event)

    def prepare(self, messages, llm=None, *, system=(), tools=(), output_tokens=4096, force=False, reactive=False):
        self.units(messages)
        result = self.receive(messages)
        budget = self.history_budget(system, tools, output_tokens)
        target = max(0, int(budget * .8))
        if reactive:
            target = min(target, int(estimate_tokens(messages) * .5))
        if estimate_tokens(messages) > budget:
            self.micro_compact(messages, target, result)
            if estimate_tokens(messages) > target:
                self.compact_history(messages, llm, target, result)
        elif force:
            self.compact_history(messages, llm, target, result, force_summary=True)
        self._finish(messages, result)
        if result.after_tokens > budget:
            raise ContextLimitError(
                f"Context needs {result.after_tokens} tokens; history budget is {budget}. "
                f"Protected request/tool calls were preserved. {result.error or ''}"
            )
        return result

    def export_state(self, messages):
        self._sync(messages)
        return {"version": 1, "conversation_id": self.store.conversation_id,
                "agent_id": self.store.agent_id, "active_id": self.active_id,
                "messages": [dict(self._meta(m)) for m in messages]}

    def restore_state(self, messages, state=None):
        self._records = {}
        self.active_id = None
        self._last_snip = None
        if state:
            if state.get("version") != 1 or len(state.get("messages", [])) != len(messages):
                raise ValueError("Invalid saved context metadata")
            self.store = TranscriptStore(state["conversation_id"], state["agent_id"])
            for m, meta in zip(messages, state["messages"]):
                checked_id(meta["id"])
                if meta.get("original"):
                    checked_id(meta["original"])
                self._records[id(m)] = (m, dict(meta))
            self.active_id = state.get("active_id")
            if self.active_id is not None:
                checked_id(self.active_id)
        else:
            # Conservatively treat imported tool results as unread.
            self.store = TranscriptStore()
            for m in reversed(messages):
                if m.get("role") == "user":
                    self.begin_turn(m)
                    break
        self._sync(messages)

    def reset(self):
        self.restore_state([])
