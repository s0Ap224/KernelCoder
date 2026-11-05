"""MCP stdio client, distilled to the slice of the protocol an agent uses.

Servers are configured in ~/.kernelcoder/mcp.json:

    {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "some-fs-server", "/tmp"]}}}

Each server is a subprocess speaking JSON-RPC 2.0, one message per line over
stdin/stdout. Startup handshakes (`initialize`), pulls `tools/list`, and every
remote tool joins the agent as `mcp__<server>__<tool>`, so consent, hooks and
the main loop treat them exactly like the built-ins. A server that hangs or
dies fails that one call as an ordinary error string; it never kills the loop.
The REPL's /mcp shows each server's state and reconnects a dead one by hand.
"""

import atexit
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from . import __version__
from .tools.base import Tool

log = logging.getLogger(__name__)

CONFIG_FILE = Path.home() / ".kernelcoder" / "mcp.json"
PROTOCOL_VERSION = "2025-06-18"
INIT_TIMEOUT = 15  # seconds for initialize + tools/list at startup
CALL_TIMEOUT = 60  # seconds for one tools/call
MAX_MESSAGE = 8 * 1024 * 1024


class MCPError(RuntimeError):
    """Transport or protocol failure talking to one server."""


class MCPClient:
    """One stdio server process: handshake, list, call, shut down.

    A daemon thread owns stdout and parks each response under its request id,
    so calls from parallel tool execution match their own replies; the write
    lock keeps two threads' requests from interleaving on stdin.
    """

    def __init__(self, name: str, command: str, args: list = (), env: dict | None = None,
                 *, cwd=None, managed=False):
        self.name = name
        self.call_timeout = CALL_TIMEOUT
        self._command = command
        self._args = args
        self._env = env
        self._cwd = cwd
        self._managed = managed
        self._tree = None
        self._proc = None
        self.stderr = ""
        self._pending = set()
        self._next_id = 0
        self._dead: MCPError | None = None
        self._responses: dict[int, dict] = {}
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._start()

    def _start(self):
        """Spawn the server and run the startup handshake."""
        from .mcp_process import ProcessTree
        with self._cond:
            self._dead = None
            self._responses.clear()
            self._pending.clear()
        self.stderr = ""
        if self._managed:
            self._tree = ProcessTree()
        spawn = self._tree.start if self._tree else subprocess.Popen
        try:
            self._proc = spawn(
                [self._command, *self._args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=self._env if self._managed else {**os.environ, **(self._env or {})},
                cwd=self._cwd,
                stderr=subprocess.PIPE if self._managed else None,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # the transport is newline-delimited JSON
            )
        except BaseException:
            if self._tree:
                self._tree.close()
                self._tree = None
            raise
        if self._managed:
            self._stderr_thread = threading.Thread(target=self._stderr_loop, args=(self._proc,), daemon=True)
            self._stderr_thread.start()
        threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True).start()
        try:
            initialized = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "kernelcoder", "version": __version__},
            }, INIT_TIMEOUT)
            if initialized.get("protocolVersion") != PROTOCOL_VERSION:
                raise MCPError(f"Unsupported MCP protocol: {initialized.get('protocolVersion')}")
            if "tools" not in initialized.get("capabilities", {}):
                raise MCPError("MCP server does not support tools")
            self.server_info = initialized.get("serverInfo", {})
            self._notify("notifications/initialized")
            specs, seen, params = [], set(), {}
            for _ in range(100):
                listed = self._request("tools/list", params, INIT_TIMEOUT)
                specs.extend(listed.get("tools", []))
                cursor = listed.get("nextCursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise MCPError("Repeated tools/list cursor")
                seen.add(cursor)
                params = {"cursor": cursor}
            else:
                raise MCPError("Too many tools/list pages")
            self.tool_specs = specs
            self.tools = [MCPTool(self, t) for t in specs]
        except BaseException as e:
            self.close()  # a half-started server must not leak
            if isinstance(e, MCPError) and self.stderr:
                raise MCPError(f"{e}\n{self.stderr}") from e
            raise

    @property
    def error(self) -> MCPError | None:
        """What killed the server, or None while it is healthy."""
        return self._dead

    def reconnect(self):
        """Start a fresh process for a dead or hung server and redo the
        handshake. The mcp__ tools the agent already holds route through this
        client, so they work again as soon as the handshake lands."""
        self.close()
        try:
            self._start()
        except (MCPError, OSError) as e:
            with self._cond:
                self._dead = e if isinstance(e, MCPError) else MCPError(str(e))
            raise

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Run one remote tool and return its text content."""
        result = self.call_result(tool_name, arguments)
        text = "\n".join(
            part.get("text", "")
            for part in result.get("content", [])
            if part.get("type") == "text"
        )
        if result.get("isError"):
            raise MCPError(text or f"{tool_name} reported an error")
        return text or json.dumps(result)  # non-text content: hand the model the raw result

    def call_result(self, tool_name, arguments):
        """Preserve all content blocks and structuredContent for managed jobs."""
        return self._request("tools/call", {"name": tool_name, "arguments": arguments}, self.call_timeout)

    def close(self):
        """Shut the server down. Safe to call twice."""
        if self._proc is None:
            if self._tree:
                self._tree.close()
            return
        if self._tree:
            self._tree.stop(self._proc)
            self._tree = None
            self._stderr_thread.join(timeout=2)
        if self._proc.poll() is not None:
            return
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)

    def _notify(self, method: str):
        try:
            self._write({"jsonrpc": "2.0", "method": method, "params": {}})
        except MCPError:
            pass  # a notification has no reply to lose; the next request meets the dead server

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        with self._cond:
            if self._dead is not None:
                raise self._dead
            self._next_id += 1
            req_id = self._next_id
            self._pending.add(req_id)
        try:
            self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            deadline = time.monotonic() + timeout
            with self._cond:
                while req_id not in self._responses:
                    if self._dead is not None:
                        raise self._dead
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise MCPError(f"MCP server {self.name!r} gave no answer to {method} in {timeout:g}s")
                    self._cond.wait(left)
                msg = self._responses.pop(req_id)
        finally:
            with self._cond:
                self._pending.discard(req_id)
                self._responses.pop(req_id, None)
        if "error" in msg:
            error = msg["error"]
            message = error.get("message", error) if isinstance(error, dict) else error
            raise MCPError(f"MCP server {self.name!r} rejected {method}: {message}")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise MCPError("MCP result must be an object")
        return result

    def _write(self, msg: dict):
        encoded = json.dumps(msg) + "\n"
        if len(encoded.encode("utf-8")) > MAX_MESSAGE:
            raise MCPError("MCP request exceeds size limit")
        try:
            with self._write_lock:
                self._proc.stdin.write(encoded)
                self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise MCPError(f"MCP server {self.name!r} is not writable: {e}") from e

    def _read_loop(self, proc: subprocess.Popen):
        try:
            while line := proc.stdout.readline(MAX_MESSAGE + 1):
                if len(line) > MAX_MESSAGE:
                    raise MCPError("MCP message exceeds size limit")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("MCP server %r sent a non-JSON line, skipped", self.name)
                    continue
                if not isinstance(msg, dict) or "id" not in msg:
                    continue  # server notification: nothing here needs an answer
                if "method" in msg:
                    if proc is self._proc:
                        self._write({"jsonrpc": "2.0", "id": msg["id"],
                                     "error": {"code": -32601, "message": "Client capability not supported"}})
                    continue
                with self._cond:
                    if isinstance(msg["id"], int) and msg["id"] in self._pending and proc is self._proc:
                        self._responses[msg["id"]] = msg
                    self._cond.notify_all()
        except (OSError, ValueError, MCPError) as e:
            with self._cond:
                if proc is self._proc:
                    self._dead = MCPError(str(e))
        with self._cond:
            # stdout closing means the server is gone for good; a reader left
            # over from before a reconnect must not mark the new process dead
            if proc is self._proc and self._dead is None:
                self._dead = MCPError(f"MCP server {self.name!r} exited")
            self._cond.notify_all()

    def _stderr_loop(self, proc):
        while chunk := proc.stderr.read(1024):
            self.stderr = (self.stderr + chunk)[-16000:]


class MCPTool(Tool):
    """A tool living in an MCP server, registered under a mcp__ name so it can
    never collide with a built-in. Side effects are unknown, so it stays out of
    Permission.READ_ONLY and the consent gate asks first, like any mutating tool.
    """

    def __init__(self, client: MCPClient, spec: dict):
        self._client = client
        self._remote_name = spec["name"]
        self.name = f"mcp__{client.name}__{spec['name']}"
        self.description = spec.get("description") or ""
        self.parameters = spec.get("inputSchema") or {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> str:
        return self._client.call_tool(self._remote_name, kwargs)


_live_clients: list[MCPClient] = []


def load_mcp_tools(path: Path = CONFIG_FILE) -> list[Tool]:
    """Start the configured servers and return their tools. A missing file
    means no MCP; a broken file or a server that won't start gets one clear
    warning and the agent carries on with whatever loaded."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        log.warning("ignoring %s: %s", path, e)
        return []
    tools: list[Tool] = []
    for name, spec in (data.get("mcpServers") or {}).items():
        try:
            client = MCPClient(name, spec["command"], spec.get("args", []), spec.get("env"))
        except (MCPError, OSError, KeyError, TypeError) as e:
            log.warning("MCP server %r skipped: %s", name, e)
            continue
        _live_clients.append(client)
        tools.extend(client.tools)
    return tools


@atexit.register
def _shutdown():
    for client in _live_clients:
        client.close()
