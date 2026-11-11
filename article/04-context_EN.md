# 04: Recoverable four-stage context compaction

KernelCoder prepares context before each model request. `context.py` manages the working messages and budget; `transcripts.py` stores omitted material; `read_transcript` provides bounded, agent-scoped retrieval.

| Stage | Trigger | Behavior |
| --- | --- | --- |
| tool_budget_constraint | Latest tool batch exceeds 100,000 characters | Persist results larger than 20,000 characters, largest first, leaving a 2,000-character preview and reference |
| snip_compact | More than 40 messages | Select positions 1, 2, 11, 20 and the last 15; expand tool batches and protect active/unread input; archive omitted messages |
| micro_compact | Estimated request exceeds the available budget | Protect the latest five tool results and all unread batches; shorten older results to 150-character previews, oldest first, targeting 80% of the available budget |
| compact_history | Earlier steps cannot reach the target, or manual /compact | Snapshot all messages, fit oversized results using 2,000/150-character previews or references, then summarize eligible history if necessary |

The batch threshold is a trigger, not a guaranteed final size. The 20-message target is soft because complete batches and protected input take priority. Redundant or unhelpful cuts are skipped.

## Message boundaries

The wire format remains OpenAI-compatible: an assistant tool_calls message followed by exactly one tool result for every call ID. A batch is indivisible. Ordinary user messages are valid independent boundaries. Every incoming message runs the cheap checks; history removal waits until the batch closes. Before sending a request, the full message sequence is validated.

The active user request remains verbatim and appears once. Unread tool batches are excluded from the summary input; only a successful primary model response marks results as seen. Summary calls do not change that state. Interrupted calls receive placeholder replies before another request is sent.

## Budget and failure behavior

The default context window is 128,000 tokens. The history allowance subtracts the system prompt, task/mode state, tool schemas, output reservation and a 5% safety margin. Protected requests, call arguments and archive markers all count toward this allowance.

COMPACT_PROMPT asks for factual goals, files, decisions, remaining work, constraints and observed verification. Historical instructions are data, not commands. Summary input is bounded and favors existing summaries and recent history; output is capped at 2,000 tokens, reduced further for small windows, using a per-call override. Empty, failed or non-shrinking summaries never replace history. No synthetic assistant acknowledgment is inserted.

Archival must succeed before text is replaced. If protected input still cannot fit, ContextLimitError reports the problem without silently truncating the request. An explicit provider context-overflow error permits one emergency compaction and model retry; completed tools are never re-executed.

## Archives and sessions

Immutable artifacts live under ~/.kernelcoder/transcripts/<conversation_id>/<agent_id>/ outside the project tree. Records identify the compaction stage, source message/call, original artifact and sizes. Later transformations retain the original reference. Only content actually returned to the Agent is recoverable; truncation inside a tool predates this archive.

read_transcript(artifact_id, offset=0, limit=2000) returns character pages, capped at 10,000 content characters. IDs are scoped to the current agent; arbitrary paths and linked artifacts are rejected. This dedicated read-only capability works in host and sandbox modes without granting general host filesystem access.

/save and --resume preserve the namespace, active request identity and result-read state. Legacy sessions remain readable, with imported results conservatively treated as unread. Reset rotates the namespace without deleting artifacts. Sub-agents receive separate agent IDs and separately bound readers.

See tests/test_context.py for boundary, pairing, preservation, archive, session and overflow-retry regressions.
