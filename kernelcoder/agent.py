"""Core agent loop.

This is the heart of KernelCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import concurrent.futures
import contextvars
import copy
import inspect

from .context import ContextManager
from .llm import LLM
from .mcp_local import ManagedMCPTool
from .permissions import Permission
from .prompt import PLAN_MODE_PROMPT, system_prompt
from .skills import SkillTool
from .tools import ALL_TOOLS
from .tools.agent import AgentTool
from .tools.base import Tool
from .tools.glob_tool import GlobTool
from .tools.grep import GrepTool
from .tools.read import ReadFileTool
from .tools.todo import TodoWriteTool

_PARALLEL_READ_IMPLEMENTATIONS = {
    "read_file": ReadFileTool,
    "glob": GlobTool,
    "grep": GrepTool,
    "load_skill": SkillTool,
}


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        permission=None,
        hooks=None,
        workspace=None,
    ):
        self.llm = llm
        self.workspace = workspace
        self.tools = list(tools) if tools is not None else [type(t)() for t in ALL_TOOLS]
        # A shared AgentTool must never acquire a different parent's policy.
        self.tools = [copy.copy(t) if isinstance(t, AgentTool) else t for t in self.tools]
        self.permission = permission
        self.hooks = None if workspace else hooks
        if any(type(t) is ManagedMCPTool and t.manager.workspace is not workspace for t in self.tools):
            raise ValueError("Managed MCP tools must be bound to this task workspace")
        if workspace and any(t.name.startswith("mcp__") and
                             (type(t) is not ManagedMCPTool or t.manager.workspace is not workspace)
                             for t in self.tools):
            raise ValueError("MCP tools must be managed and bound to this task workspace")
        self._tool_by_name = {t.name: t for t in self.tools}
        # 工程级设计，每个（子）agent只认它的一套工具。
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        self._system = system_prompt(self.tools, sandbox=workspace is not None)
        self.plan_mode = False  # toggled by /plan; while on, mutating tools are refused

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

        # activate todo tool
        self._todo = None
        for t in self.tools:
            if isinstance(t, TodoWriteTool):
                self._todo = t
                break

    def _full_messages(self) -> list[dict]:
        """原始message + 系统提示词 + plan_mode 提示词（潜在） + _todo信息（潜在）"""
        system = self._system
        # re-injected every round, like the task list below, so a toggle made
        # between turns takes effect on the very next request
        if self.plan_mode:
            system += "\n\n" + PLAN_MODE_PROMPT
        # the task list is re-injected every round, so the model always sees the
        # current state rather than a stale copy buried in old tool results
        if self._todo is not None:
            rendered = self._todo.render()
            if rendered:
                system += "\n\n# Current task list\n" + rendered
        return [{"role": "system", "content": system}] + self.messages

    def _tool_schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools]

    def chat(self, user_input: str, on_token=None, on_tool=None, on_reasoning=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        self.messages.append({"role": "user", "content": user_input})
        self.context.maybe_compress(self.messages, self.llm)

        for _ in range(self.max_rounds):
            resp = self.llm.chat(
                messages=self._full_messages(),
                tools=self._tool_schemas(),
                on_token=on_token,
                on_reasoning=on_reasoning,
            )

            # no tool calls -> LLM is done, return text
            if not resp.tool_calls:
                self.messages.append(resp.message)
                return resp.content

            # tool calls -> execute (parallel when multiple, like Claude Code's
            # StreamingToolExecutor which runs independent tools concurrently)
            self.messages.append(resp.message)

            try:
                if len(resp.tool_calls) == 1:
                    tc = resp.tool_calls[0]
                    if on_tool:
                        on_tool(tc.name, tc.arguments)
                    result = self._pre_hooks(tc) or self._permit(tc)
                    if result is None:
                        result = self._exec_tool(tc)
                        self._post_hooks(tc, result)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                else:
                    # parallel execution for multiple tool calls
                    results = self._exec_tools_parallel(resp.tool_calls, on_tool)
                    for tc, result in zip(resp.tool_calls, results):
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })
            except KeyboardInterrupt:
                # Ctrl+C mid-execution would leave the assistant tool_calls
                # message without replies, poisoning the next request; backfill
                self._answer_pending_tool_calls(resp.tool_calls)
                raise

            # compress if tool outputs are big
            self.context.maybe_compress(self.messages, self.llm)

        return "(reached maximum tool-call rounds)"

    def _pre_hooks(self, tc) -> str | None:
        """PreToolUse hooks, fired before consent. A string return blocks the
        call and becomes the tool result the model sees; None lets it through."""
        if self.hooks is None:
            return None
        return self.hooks.run_pre(tc.name, tc.arguments)

    def _post_hooks(self, tc, result: str):
        """PostToolUse hooks observe a finished call; they can never block."""
        if self.hooks is not None:
            self.hooks.run_post(tc.name, tc.arguments, result)

    def _permit(self, tc) -> str | None:
        """Consent check for one call. None means go ahead; a string is the
        refusal, returned as the tool result instead of executing."""
        # plan mode outranks consent, even --yes: while it's on nothing mutates
        tool = self._tool_by_name.get(tc.name)
        managed_read = type(tool) is ManagedMCPTool and tool.manager.workspace is self.workspace
        if self.plan_mode and tc.name not in Permission.READ_ONLY and not managed_read:
            return (
                "Plan mode is on, so this call was refused: plan mode is "
                "read-only. Do not retry it. Keep investigating with the "
                "read-only tools, then present the plan and stop. The user can "
                'approve it by typing "approve", or exit plan mode with /plan.'
            )
        if self.permission is None:
            return None
        return self.permission.check(tool.permission_key if managed_read else tc.name, tc.arguments)

    def _exec_tool(self, tc) -> str:
        """Execute a single call under the workspace guard, when configured."""
        try:
            if self.workspace:
                with self.workspace.guard():
                    return self._exec_tool_body(tc)
            return self._exec_tool_body(tc)
        except Exception as e:  # noqa: BLE001
            return f"Error executing {tc.name}: {e}"

    def _exec_tool_body(self, tc) -> str:
        """Shared call boundary. Workspace callers must already hold the guard.

        Only audited read batches may delegate this body to worker threads;
        the scheduling thread keeps the guard until every worker has stopped.
        """
        tool = self._tool_by_name.get(tc.name)
        if tool is None:
            return f"Error: unknown tool '{tc.name}'"
        # validate arguments first so a TypeError raised *inside* the tool isn't
        # mislabelled as a bad-arguments error from the caller
        try:
            inspect.signature(tool.execute).bind(**tc.arguments)
        except TypeError as e:
            return f"Error: bad arguments for {tc.name}: {e}"
        # a tool that blows up gets reported back as text, never kills the loop
        try:
            return tool.execute(**tc.arguments)
        except Exception as e:  # noqa: BLE001
            return f"Error executing {tc.name}: {e}"

    def _parallel_read(self, tc) -> bool:
        tool = self._tool_by_name.get(tc.name)
        return (tc.name in Permission.PARALLEL_READ_ONLY
                and type(tool) is _PARALLEL_READ_IMPLEMENTATIONS.get(tc.name))

    def _exec_workspace_reads(self, tool_calls, indexes, results):
        """Hold one guard across a contiguous read batch, including shutdown."""
        if not indexes:
            return
        try:
            with self.workspace.guard():
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(indexes)))
                futures = {}
                try:
                    for i in indexes:
                        # Context objects cannot be entered concurrently. Each
                        # worker gets its own copy, including CURRENT_WORKSPACE.
                        ctx = contextvars.copy_context()
                        future = pool.submit(ctx.run, self._exec_tool_body, tool_calls[i])
                        futures[future] = i
                    for future in concurrent.futures.as_completed(futures):
                        results[futures[future]] = future.result()
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
                    interrupted = False
                    # A second Ctrl+C must not release the guard while readers
                    # are still running. Cancelled futures are already done.
                    while any(not f.done() for f in futures):
                        try:
                            concurrent.futures.wait(
                                [f for f in futures if not f.done()], timeout=.1,
                            )
                        except KeyboardInterrupt:
                            interrupted = True
                    pool.shutdown(wait=True)
                    if interrupted:
                        raise KeyboardInterrupt
        except Exception as e:  # noqa: BLE001 -- guard/pool failure is a tool result
            for i in indexes:
                if results[i] is None:
                    results[i] = f"Error executing {tool_calls[i].name}: {e}"

    def _exec_workspace_batch(self, tool_calls, results):
        reads = []
        for i, tc in enumerate(tool_calls):
            if self._parallel_read(tc):
                if results[i] is None:
                    reads.append(i)
                continue
            # Even a denied state-changing call remains an ordering boundary.
            self._exec_workspace_reads(tool_calls, reads, results)
            reads = []
            if results[i] is None:
                results[i] = self._exec_tool(tc)
        self._exec_workspace_reads(tool_calls, reads, results)
        return results

    def _exec_tools_parallel(self, tool_calls, on_tool=None) -> list[str]:
        """Run a batch, preserving result order and main-thread consent.

        Workspaces parallelize contiguous audited reads, with state-changing
        calls as serial barriers. Host mode retains its existing thread pool.
        """
        from .tools.bash import get_tracked_cwd, set_tracked_cwd

        for tc in tool_calls:
            if on_tool:
                on_tool(tc.name, tc.arguments)

        # hooks and consent are settled up front on this thread: prompting
        # from pool workers would interleave several prompts on one terminal
        results = [self._pre_hooks(tc) or self._permit(tc) for tc in tool_calls]
        if self.workspace:
            return self._exec_workspace_batch(tool_calls, results)
        # the tracked cwd is thread-local, so pool workers would otherwise
        # start from the launch directory: hand the session cwd in, and merge
        # any cd back in call order so the batch behaves like a sequence
        session_cwd = get_tracked_cwd()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            def run(i, tc, base_cwd):
                is_bash = tc.name == "bash"
                if is_bash and base_cwd is not None:
                    set_tracked_cwd(base_cwd)

                out = self._exec_tool(tc)

                if not is_bash:
                    return i, out, None
                cwd_after = get_tracked_cwd()
                # only a worker that actually moved the dir reports back; a
                # sibling that merely inherited the base must not revert a cd
                return i, out, cwd_after if cwd_after != base_cwd else None
            # 返回 (原索引, 工具结果, 发生变化的新目录或None)


            futures = {}
            for i, tc in enumerate(tool_calls):
                if results[i] is None:
                    futures[i] = pool.submit(run, i, tc, session_cwd)

            for i, future in futures.items():
                i, results[i], worker_cwd = future.result()
                # 上行为result核心修改行
                if worker_cwd is not None:
                    session_cwd = worker_cwd
                    set_tracked_cwd(worker_cwd)

        for i in futures:
            self._post_hooks(tool_calls[i], results[i])
        return results

    def _answer_pending_tool_calls(self, tool_calls):
        """Backfill a tool reply for every call that didn't get one.

        OpenAI-compatible APIs reject a request where an assistant message has
        tool_calls without a matching tool reply for each id, so this keeps the
        history valid when execution is interrupted partway through.
        """
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for tc in tool_calls:
            if tc.id not in answered:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": "[interrupted]", #核心，‘interrupted’占位符填充未相应的内容
                })

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()
        # a user-visible reset must not leak the dead conversation's checklist
        # into the next system prompt
        todo = self._tool_by_name.get("todo_write")
        if isinstance(todo, TodoWriteTool):
            todo._tasks = []
