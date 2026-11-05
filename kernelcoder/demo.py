"""Scripted model demo: no model key; E2B mode requires an E2B key.

Runs one scripted task through the real Agent loop with a ScriptedLLM, so the
thought -> tool call -> observation cycle renders exactly as it would against
a live model. Useful for demos, screenshots, and as a smoke test of the loop.
"""

import subprocess
import tempfile
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .agent import Agent
from .cli import _brief
from .llm import LLMResponse, ToolCall

console = Console()


class ScriptedLLM:
    """Deterministic offline LLM for demos and smoke tests.

    Plays back a list of LLMResponse turns, one per chat() call, streaming
    each turn's content through on_token. Running out of turns is an error,
    not a silent hang, so a broken loop shows up immediately.
    """

    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None  # the REPL's /tokens reads this off the LLM

    def __init__(self, script: list[LLMResponse], model: str = "scripted-demo"):
        self._turns = list(script)
        self.model = model

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None) -> LLMResponse:
        if not self._turns:
            raise RuntimeError("ScriptedLLM ran out of turns")
        resp = self._turns.pop(0)
        if on_token and resp.content:
            on_token(resp.content)
        self.total_completion_tokens += len(resp.content.split())
        return resp

_TASK = (
    "Write a Python function `fib(n)` in fib.py returning the nth Fibonacci "
    "number, add a pytest file, then run the tests."
)


def _script(workdir: Path) -> list[LLMResponse]:
    fib_py = (
        "def fib(n):\n"
        "    if n < 2:\n"
        "        return n\n"
        "    a, b = 0, 1\n"
        "    for _ in range(2, n + 1):\n"
        "        a, b = b, a + b\n"
        "    return b\n"
    )
    test_py = (
        "from fib import fib\n"
        "\n"
        "def test_fib():\n"
        "    assert fib(0) == 0\n"
        "    assert fib(1) == 1\n"
        "    assert fib(10) == 55\n"
    )
    return [
        LLMResponse(
            content="I'll write fib.py with an iterative implementation.",
            tool_calls=[ToolCall(id="c1", name="write_file", arguments={"file_path": str(workdir / "fib.py"), "content": fib_py})],
        ),
        LLMResponse(
            content="Now the test file covering the base cases and fib(10).",
            tool_calls=[ToolCall(id="c2", name="write_file", arguments={"file_path": str(workdir / "test_fib.py"), "content": test_py})],
        ),
        LLMResponse(
            content="Running the tests.",
            tool_calls=[ToolCall(id="c3", name="bash", arguments={"command": f'cd "{workdir}" && python -m pytest test_fib.py -q'})],
        ),
        LLMResponse(
            content="All three assertions pass. `fib` is iterative, and the test covers the base cases plus fib(10) == 55."
        ),
    ]


def run_demo(*, unsafe=False, runner=None, store=None) -> int:
    from .sandbox import SandboxRunner
    from .tools import ALL_TOOLS
    from .tools.bash import BashTool
    from .workspace import SandboxWorkspace, WorkspaceError

    runner = runner or SandboxRunner()
    if not unsafe:
        try:
            runner.check()
        except (WorkspaceError, OSError, ValueError, subprocess.SubprocessError) as e:
            console.print(f"Sandbox unavailable: {e}", markup=False)
            return 1
    workdir = Path(tempfile.mkdtemp(prefix="kernelcoder-demo-"))
    workspace = None if unsafe else SandboxWorkspace.create(workdir, store)
    tools = [type(t)() for t in ALL_TOOLS if t.name != "bash"]
    tools.insert(0, BashTool(workspace, runner, unsafe=unsafe))
    agent = Agent(llm=ScriptedLLM(_script(Path("/workspace") if workspace else workdir)),
                  tools=tools, workspace=workspace)

    if workspace:
        # Keep the demo's dependency setup explicit, just like a normal agent.
        agent.llm._turns.insert(0, LLMResponse(tool_calls=[ToolCall(
            id='install', name='bash', arguments={'command': 'python -m pip install --user pytest -q'})]))
    console.print(Panel.fit(f"[bold]{_TASK}[/]", title="kernelcoder demo (scripted model)"))
    try:
        result = agent.chat(
            _TASK,
            on_tool=lambda name, args: console.print(f"[cyan]tool:[/] {name} {_brief(args)}"),
        )
    finally:
        if workspace:
            try:
                runner.close(workspace)
            except WorkspaceError as e:
                console.print(f'Cleanup required: {e}', markup=False)
    console.print(Panel.fit(Markdown(result), title="final"))
    console.print(f"[dim]workspace kept at {workdir}[/]")
    if workspace:
        console.print(workspace.review(), markup=False)
        console.print(f"Sandbox session: {workspace.id}")
    # The final response is scripted; only tool results establish execution success.
    if any(m.get("role") == "tool" and ("Error" in m["content"]
           or any(f'[{status}' in m['content'] for status in ('runtime_error', 'policy_denied', 'sync_error', 'timeout', 'cancelled'))
           or ('[completed; exit=' in m['content'] and '[completed; exit=0]' not in m['content'])
           or '[exit code:' in m['content']) for m in agent.messages):
        return 1
    return 0
