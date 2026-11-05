"""Regenerate curated input schemas from an explicitly installed pinned runtime.

Run from the repository: python scripts/lock_mcp_profiles.py --python PATH
This starts local services, but never connects to E2B.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kernelcoder.mcp import MCPClient
from kernelcoder.mcp_local import DATA, GIT_TOOLS, PINS, LocalMCPRunner, clean_environment, server_args


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    python = str(Path(args.python).absolute())
    profiles = {}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        env = clean_environment(root, python)
        env["SEMGREP_MCP_DISABLE_TRACING"] = "true"
        LocalMCPRunner(None, python, {})._versions(env)
        for name, wanted, package in (
            ("git", GIT_TOOLS, "mcp-server-git"),
            ("semgrep", ("semgrep_scan_with_custom_rule",), "semgrep"),
        ):
            argv = server_args(name)
            client = MCPClient(name, python, argv, env, cwd=root, managed=True)
            try:
                specs = {s["name"]: s["inputSchema"] for s in client.tool_specs}
                profiles[name] = {"package": package, "version": PINS[package],
                                  "tools": {tool: specs[tool] for tool in wanted}}
                print(name, client.server_info, sorted(profiles[name]["tools"]))
            finally:
                client.close()
    DATA.mkdir(exist_ok=True)
    (DATA / "profiles.json").write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
