"""Interactive REPL - the user-facing terminal interface."""

import argparse
import subprocess
import sys
from pathlib import Path

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import __version__
from .agent import Agent
from .config import Config, environment_scope, load_settings, user_dir
from .hooks import load_hooks
from .llm import LLM, LiteLLM
from .mcp import load_mcp_tools
from .mcp_local import MCPManager
from .permissions import DEFAULT_STORE, Permission
from .sandbox import SandboxRunner
from .session import list_sessions, load_context_state, load_session, save_session, session_sandbox_id
from .skills import SkillLoader, SkillTool
from .tools import ALL_TOOLS
from .tools.bash import BashTool
from .workspace import SandboxWorkspace, WorkspaceError

console = Console() #创建一个 Rich 的 Console 实例，默认输出到 stdout
err_console = Console(stderr=True) #让这个实例把内容输出到 stderr，而不是 stdout


def _parse_args():
    p = argparse.ArgumentParser(
        prog="kernelcoder",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("command", nargs="?", choices=("init", "doctor", "setup-mcp"), help="First-run setup or offline diagnostics")
    p.add_argument("--from-env", type=Path, help="Import supported variables during init")
    p.add_argument("--env-file", type=Path, help="Explicit environment file, above project/global files")
    p.add_argument("--mode", choices=("local", "e2b"), help="Execution mode (otherwise use saved default)")
    # 解析命令行参数
    p.add_argument("-m", "--model", help="Model name (default: $KERNELCODER_MODEL or gpt-5.5)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("--permission-mode", choices=("default", "acceptEdits", "bypass"))
    p.add_argument("--yes", action="store_true", help="Bypass ordinary prompts; explicit ask and deny still apply")
    p.add_argument("--demo", action="store_true", help="Run a scripted model demo (E2B key needed unless unsafe)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    p.add_argument("--sandbox", choices=("e2b", "unsafe"),
                   help="Execute in E2B (default); unsafe explicitly runs on the host")
    p.add_argument("--mcp-backend", choices=("local", "off"),
                   help="Curated MCP backend (stage 2: local or off; saved with the task)")
    p.add_argument("--mcp-python", type=Path, help="Python in the pinned MCP environment; requires local MCP")
    p.add_argument("--sandbox-check", action="store_true", help="Create, test and destroy E2B; E2B key needed, no model key")
    p.add_argument("--sandbox-session", help="Reopen a temporary workspace by its session ID")
    p.add_argument("--sandbox-store", type=Path, help="Host metadata directory outside the project")
    p.add_argument("--sandbox-exclude", action="append", default=[], help="Additional copy exclusion glob; repeatable")
    p.add_argument("--sandbox-action", choices=("status", "diff", "apply", "discard", "cleanup"),
                   help="Manage --sandbox-session without an API key; diff required before apply")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()
    if args.yes and args.permission_mode not in (None, "bypass"):
        p.error("--yes conflicts with --permission-mode")
    args.mode_explicit = args.mode is not None or args.sandbox is not None
    selected = "unsafe" if args.mode == "local" else "e2b" if args.mode == "e2b" else None
    if selected and args.sandbox and selected != args.sandbox:
        p.error("--mode conflicts with --sandbox")
    args.sandbox = selected or args.sandbox or "e2b"
    if args.from_env and args.command != "init":
        p.error("--from-env is only supported with init")
    return args


def main():
    args = _parse_args()
    with environment_scope(getattr(args, "env_file", None)):
        try:
            _main(args)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            # Validation errors are deliberately value-free. Subprocess output may contain secrets.
            message = str(e) if isinstance(e, ValueError) else "Filesystem or subprocess operation failed."
            console.print(f"Setup/configuration error: {message}", markup=False)
            raise SystemExit(1) from None
        except (EOFError, KeyboardInterrupt):
            console.print("Cancelled.")
            raise SystemExit(130) from None


def _main(args):
    from .onboarding import doctor, initialize, setup_mcp

    command = getattr(args, "command", None)
    if command == "init":
        initialize(console, args.from_env, args.mode)
        return
    if command == "setup-mcp":
        setup_mcp(console)
        return
    settings = load_settings()
    # An existing sandbox session determines the implicit mode before user defaults.
    if not getattr(args, "mode_explicit", True):
        resumed = args.sandbox_session or (session_sandbox_id(args.resume) if args.resume else None)
        mode = "e2b" if resumed else settings.get("mode", "e2b")
        args.sandbox = "unsafe" if mode == "local" else "e2b"
    if command == "doctor":
        raise SystemExit(doctor(console, args, settings))

    runner = SandboxRunner()
    try:
        if args.sandbox_check:
            console.print(runner.probe(), markup=False)
            return
        if args.sandbox_action:
            if not args.sandbox_session:
                raise WorkspaceError("--sandbox-action requires --sandbox-session")
            workspace = SandboxWorkspace.open(args.sandbox_session, args.sandbox_store)
            _sandbox_action(workspace, runner, args.sandbox_action)
            return
    except (WorkspaceError, OSError, ValueError, subprocess.SubprocessError) as e:
        console.print(f"Sandbox unavailable: {e}", markup=False)
        raise SystemExit(1) from e

    if args.demo:
        from .demo import run_demo
        raise SystemExit(run_demo(unsafe=args.sandbox == "unsafe", runner=runner, store=args.sandbox_store))

    config = Config.from_env()

    if args.sandbox == "unsafe" and sys.platform == "win32":
        from .shell import _git_bash
        if not _git_bash():
            raise ValueError("Local mode requires Git Bash. Install Git for Windows, then run kernelcoder doctor.")

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, DEEPSEEK_API_KEY, or KERNELCODER_API_KEY\n"
            "\nExamples:\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 KERNELCODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    skill_loader = SkillLoader()
    skill_tools = [SkillTool(skill_loader)] if skill_loader.skills else []
    workspace = None
    try:
        if args.sandbox == "e2b":
            sid = args.sandbox_session or (session_sandbox_id(args.resume) if args.resume else None)
            workspace = (SandboxWorkspace.open(sid, args.sandbox_store) if sid else
                         SandboxWorkspace.create(Path.cwd(), args.sandbox_store, args.sandbox_exclude))
            if workspace.meta.get("active"):
                runner.cleanup(workspace)
            console.print(f"Sandbox session: {workspace.id}\nCopy: {workspace.tree}", markup=False)
        elif args.sandbox_session or (args.resume and session_sandbox_id(args.resume)):
            raise WorkspaceError("Cannot reopen a sandbox session in unsafe mode")
        else:
            console.print("UNSAFE mode: commands and file tools operate directly on the host.", markup=False)
    except (WorkspaceError, OSError, ValueError, subprocess.SubprocessError) as e:
        console.print(f"Sandbox unavailable: {e}", markup=False)
        raise SystemExit(1) from e
    tools = [type(t)() for t in ALL_TOOLS if t.name != "bash"]
    tools.insert(0, BashTool(workspace, runner, unsafe=args.sandbox == "unsafe"))
    manager = None
    try:
        if workspace:
            with workspace.guard():
                saved = workspace.meta.get("mcp", {})
                defaults = settings.get("mcp", {})
                backend = args.mcp_backend or saved.get("backend", defaults.get("backend", "off"))
                python = str(Path(args.mcp_python or saved.get("python") or defaults.get("python") or sys.executable).absolute())
                if backend not in ("local", "off"):
                    raise WorkspaceError(f"Unsupported saved MCP backend: {backend}; no host fallback")
                if args.mcp_python and backend != "local":
                    raise WorkspaceError("--mcp-python requires --mcp-backend local")
                if backend == "local":
                    manager = MCPManager(workspace, python)
                    console.print(manager.status(), markup=False)
                workspace.meta["mcp"] = {"backend": backend, "python": python}
                workspace._save()
        elif args.mcp_backend == "local" or args.mcp_python:
            raise WorkspaceError("Managed local MCP requires a task copy; omit --sandbox unsafe")
    except (WorkspaceError, OSError, ValueError) as e:
        console.print(f"MCP unavailable: {e}", markup=False)
        raise SystemExit(1) from e
    permission = Permission(
        ask=None if args.prompt else _ask_permission,
        mode="bypass" if args.yes else (getattr(args, "permission_mode", None) or "default"),
        persist_path=DEFAULT_STORE,
        project_root=workspace.original if workspace else Path.cwd(),
    )
    agent = Agent(
        llm=llm,
        tools=[*tools, *skill_tools, *(manager.tools if manager else
                                     load_mcp_tools() if workspace is None and args.mcp_backend != "off" else [])],
        max_context_tokens=config.max_context_tokens,
        permission=permission,
        hooks=load_hooks() if workspace is None else None,
        workspace=workspace,
    )
    agent.mcp_manager = manager

    # resume saved session
    if args.resume:
        loaded = load_session(args.resume)
        if loaded:
            agent.messages, loaded_model = loaded
            agent.context.restore_state(agent.messages, load_context_state(args.resume))
            # restore the model from the saved session unless overridden by CLI
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    # one-shot mode
    if args.prompt:
        try:
            _run_once(agent, args.prompt)
        finally:
            if workspace:
                _finish_sandbox(agent, config, args.sandbox_store)
        return

    # interactive REPL
    try:
        _repl(agent, config)
    finally:
        if workspace:
            _finish_sandbox(agent, config, args.sandbox_store)


def _sandbox_action(workspace, runner, action):
    if action == "status":
        text = workspace.status()
    elif action == "cleanup":
        text = runner.cleanup(workspace)
    else:
        text = getattr(workspace, {"diff": "review"}.get(action, action))()
    # Untrusted file content must not be interpreted as Rich markup.
    _print_sandbox(text)


def _print_sandbox(text):
    # Render terminal controls literally so an untrusted diff cannot erase/hide lines.
    controls = {i: f"\\x{i:02x}" for i in range(32) if i not in (9, 10)}
    controls[127] = "\\x7f"
    console.print(text.translate(controls), markup=False, highlight=False)


def _finish_sandbox(agent, config, store):
    workspace = agent.workspace
    try:
        console.print(agent._tool_by_name['bash'].runner.close(workspace), markup=False)
    except WorkspaceError as e:
        console.print(f"Sandbox cleanup required: {e}", markup=False)
    sid = save_session(agent.messages, config.model, sandbox_id=workspace.id,
                       context_state=agent.context.export_state(agent.messages))
    console.print(f"Conversation saved: {sid}; sandbox: {workspace.id}", markup=False)
    if workspace.meta["state"] == "open":
        try:
            diff = workspace.review()
            _print_sandbox(diff)
        except (WorkspaceError, OSError) as e:
            console.print(f"Review unavailable: {e}", markup=False)
        extra = f' --sandbox-store "{store}"' if store else ""
        console.print(f"Review: kernelcoder --sandbox-session {workspace.id}{extra} --sandbox-action diff\n"
                      f"Write back explicitly: kernelcoder --sandbox-session {workspace.id}{extra} --sandbox-action apply",
                      markup=False)


def _ask_permission(tool_name, arguments, *, decision=None, context=None):
    context = context or {}
    console.print(f"\nPermission requested: {tool_name}", markup=False)
    for key, value in arguments.items():
        console.print(f"{key}: {value}", markup=False)
    console.print(f"Execution: {context.get('execution', 'local')}", markup=False)
    if decision:
        console.print(decision.reason, markup=False)
    reusable = bool(decision and decision.reusable)
    options = "[y] allow once  [n] deny"
    if reusable:
        console.print(f"Grant: exact bash command for this project, cwd and execution backend: {arguments['command']}", markup=False)
        options += "  [s] allow for session  [p] save for project"
    try:
        answer = pt_prompt(options + ": ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "deny"
    if answer in {"y", "yes"}:
        return "once"
    if reusable and answer in {"s", "p"}:
        return "session" if answer == "s" else "project"
    return "deny"


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    perm = agent.permission
    if perm is not None and perm.ask is None and not perm.allow_all:
        console.print("[dim]one-shot mode: mutating tools are refused unless you pass --yes[/dim]")

    def on_token(tok):
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    def on_reasoning(tok):
        # thinking goes to stderr so piped stdout stays the plain answer
        err_console.print(f"[dim]{tok}[/dim]", end="")

    try:
        agent.chat(prompt, on_token=on_token, on_tool=on_tool, on_reasoning=on_reasoning)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        # one-shot mode: print whatever went wrong and exit non-zero
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
    print()


def _repl(agent: Agent, config: Config):
    """Interactive read-eval-print loop."""
    perm = agent.permission
    mode = f"permission mode: {perm.mode}" if perm else "default permissions"
    mcp_count = sum(1 for t in agent.tools if t.name.startswith("mcp__"))
    console.print(Panel(
        f"[bold]KernelCoder[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + f"\nPermissions: [cyan]{mode}[/cyan]"
        + (f"\nHooks: [cyan]{len(agent.hooks.pre)} pre, {len(agent.hooks.post)} post[/cyan]"
           " from ~/.kernelcoder/hooks.json" if agent.hooks else "")
        + (f"\nMCP: [cyan]{mcp_count} tools[/cyan] from ~/.kernelcoder/mcp.json" if mcp_count else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))
    console.print(f"Project: {Path.cwd()}\nExecution: {'E2B Linux (task copy)' if agent.workspace else 'local host (original project)'}\n"
                  f"Managed MCP: {'local' if getattr(agent, 'mcp_manager', None) else 'off'}; total MCP tools: {mcp_count}",
                  markup=False)
    hist_path = user_dir() / "history"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    old_history = Path(__file__).resolve().parent.parent / ".kernelcoder_history"
    if not hist_path.exists() and old_history.is_file():
        try:
            hist_path.write_bytes(old_history.read_bytes())
        except OSError:
            pass
    history = FileHistory(str(hist_path))

    # Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)
    kb = KeyBindings() #键位绑定器

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    while True:
        try:
            user_input = pt_prompt(
                "You (plan) > " if agent.plan_mode else "You > ",
                history=history,
                multiline=True,
                key_bindings=kb,
                prompt_continuation="...  ",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            break

        if not user_input:
            continue

        # built-in commands
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        if user_input == "/help":
            _show_help()
            continue
        if user_input == "/sandbox" or user_input.startswith("/sandbox "):
            action = user_input.partition(" ")[2] or "status"
            if not agent.workspace:
                console.print("UNSAFE mode: no sandbox session.")
            elif action not in {"status", "diff", "apply", "discard", "cleanup"}:
                console.print("Usage: /sandbox status|diff|apply|discard|cleanup")
            else:
                try:
                    bash = agent._tool_by_name["bash"]
                    _sandbox_action(agent.workspace, bash.runner, action)
                except (WorkspaceError, OSError, ValueError, subprocess.SubprocessError) as e:
                    console.print(f"Sandbox: {e}", markup=False)
            continue
        if user_input in {"/permissions", "/permissions reset"}:
            try:
                if user_input.endswith(" reset"):
                    agent.permission.reset()
                console.print(agent.permission.describe(), markup=False)
            except OSError as exc:
                console.print(f"Permission store error: {exc}", markup=False)
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[yellow]Conversation reset.[/yellow]")
            continue
        if user_input == "/plan":
            agent.plan_mode = not agent.plan_mode
            if agent.plan_mode:
                console.print(
                    "[yellow]Plan mode on.[/yellow] The agent can look but not touch: it will "
                    "investigate read-only and present a plan. Type [bold]approve[/bold] to "
                    "accept the plan, or [bold]/plan[/bold] again to exit."
                )
            else:
                console.print("[yellow]Plan mode off.[/yellow]")
            continue
        if agent.plan_mode and user_input.lower() in ("approve", "/approve"):
            agent.plan_mode = False
            console.print("[yellow]Plan mode off.[/yellow]")
            user_input = "approve"  # the approval itself goes to the model, which then executes
        if user_input == "/tokens":
            p = agent.llm.total_prompt_tokens
            c = agent.llm.total_completion_tokens
            line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
            cost = agent.llm.estimated_cost
            if cost is not None:
                line += f"  (~${cost:.4f})"
            console.print(line)
            continue
        if user_input == "/model" or user_input.startswith("/model "):
            new_model = user_input[7:].strip() if user_input.startswith("/model ") else ""
            if new_model:
                agent.llm.model = new_model
                config.model = new_model
                console.print(f"Switched to [cyan]{new_model}[/cyan]")
            else:
                console.print(f"Current model: [cyan]{config.model}[/cyan]")
            continue
        if user_input == "/compact":
            from .context import estimate_tokens
            before = estimate_tokens(agent.messages)
            try:
                report = agent.prepare_context(force=True)
            except (ValueError, OSError) as error:
                console.print(f"Context: {error}", markup=False)
                continue
            compressed = report.changed
            after = estimate_tokens(agent.messages)
            if compressed:
                console.print(f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages)[/green]")
            else:
                console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")
            if report.error:
                console.print(f"Context: {report.error}", markup=False)
            continue
        if user_input == "/save":
            sid = save_session(agent.messages, config.model, sandbox_id=agent.workspace.id if agent.workspace else None,
                               context_state=agent.context.export_state(agent.messages))
            console.print(f"[green]Session saved: {sid}[/green]")
            console.print(f"Resume with: kernelcoder -r {sid}")
            continue
        if user_input == "/diff":
            if agent.workspace:
                try:
                    _print_sandbox(agent.workspace.review())
                except (WorkspaceError, OSError) as e:
                    console.print(f"Sandbox: {e}", markup=False)
                continue
            from .tools.edit import _changed_files
            if not _changed_files:
                console.print("[dim]No files modified this session.[/dim]")
            else:
                console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
                for f in sorted(_changed_files):
                    console.print(f"  [cyan]{f}[/cyan]")
            continue
        if user_input == "/undo":
            if agent.workspace:
                try:
                    console.print(agent.workspace.undo(), markup=False)
                except (WorkspaceError, OSError) as e:
                    console.print(f"Sandbox: {e}", markup=False)
                continue
            from .checkpoints import pending, undo
            console.print(undo())
            left = pending()
            if left:
                console.print(f"[dim]{left} more checkpoint(s) on the stack.[/dim]")
            continue
        if user_input == "/sessions":
            sessions = list_sessions()
            if not sessions:
                console.print("[dim]No saved sessions.[/dim]")
            else:
                for s in sessions:
                    console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}) {s['preview']}")
            continue
        if user_input == "/mcp" or user_input.startswith("/mcp "):
            if agent.workspace:
                manager = getattr(agent, "mcp_manager", None)
                if user_input.strip() == "/mcp":
                    console.print(manager.status() if manager else
                                  "Managed MCP is off; start with --mcp-backend local.", markup=False)
                else:
                    console.print("Managed MCP uses a fresh process for each call; /mcp shows status.")
            else:
                _mcp(user_input[4:].strip())
            continue

        # an unknown /command shouldn't be sent to the model as a prompt
        if user_input.startswith("/"):
            console.print(f"[yellow]Unknown command: {user_input.split()[0]} (try /help)[/yellow]")
            continue

        # call the agent
        streamed: list[str] = []

        def on_token(tok, streamed=streamed):
            streamed.append(tok)
            print(tok, end="", flush=True)

        def on_tool(name, kwargs):
            console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

        def on_reasoning(tok, streamed=streamed):
            # dim the thinking inline; mark the stream non-empty so the final
            # newline logic below still fires even if the reply itself is empty
            streamed.append(tok)
            console.print(f"[dim]{tok}[/dim]", end="")

        try:
            response = agent.chat(user_input, on_token=on_token, on_tool=on_tool,
                                  on_reasoning=on_reasoning)
            if streamed:
                print()  # newline after streamed tokens
            else:
                # response wasn't streamed (came after tool calls)
                console.print(Markdown(response))
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:  # noqa: BLE001
            # keep the REPL alive no matter what chat() throws
            console.print(f"\n[red]Error: {e}[/red]")


def _mcp(arg: str):
    """The /mcp command: server states, or `reconnect <name>` to revive one."""
    from . import mcp
    parts = arg.split()
    if not parts:
        if not mcp._live_clients:
            console.print("[dim]No MCP servers running (configure ~/.kernelcoder/mcp.json).[/dim]")
        for client in mcp._live_clients:
            if client.error is None:
                console.print(f"  [cyan]{client.name}[/cyan]: alive, {len(client.tools)} tools")
            else:
                console.print(
                    f"  [cyan]{client.name}[/cyan]: [red]dead[/red] ({client.error})"
                    f", revive with /mcp reconnect {client.name}"
                )
        return
    if len(parts) == 2 and parts[0] == "reconnect":
        client = next((c for c in mcp._live_clients if c.name == parts[1]), None)
        if client is None:
            console.print(f"[red]No MCP server named '{parts[1]}'.[/red]")
            return
        try:
            client.reconnect()
        except (mcp.MCPError, OSError) as e:
            console.print(f"[red]Reconnect failed:[/red] {e}")
        else:
            console.print(f"[green]Reconnected:[/green] [cyan]{client.name}[/cyan], {len(client.tools)} tools")
        return
    console.print("[yellow]Usage: /mcp [reconnect <name>][/yellow]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /permissions   Show project grants; /permissions reset clears them\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /sandbox      status|diff|apply|discard|cleanup (apply is explicit, never --yes)\n"
        "  /undo          Revert the most recent file change\n"
        "  /plan          Toggle plan mode: read-only, then a plan to approve\n"
        "  /save          Save session to disk\n"
        "  /sessions      List saved sessions\n"
        "  /mcp           MCP server status; /mcp reconnect <name> revives a dead one\n"
        "  quit           Exit KernelCoder\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="KernelCoder Help",
        border_style="dim",
    ))


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")
