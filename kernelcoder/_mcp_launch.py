"""Wait for job assignment before launching a Windows MCP child."""

import os
import subprocess
import sys

if __name__ == "__main__":
    # Read exactly the gate bytes: buffered stdin could consume the first RPC.
    gate = b""
    while not gate.endswith(b"\n"):
        char = os.read(0, 1)
        if not char:
            sys.exit(1)
        gate += char
    if gate.strip() != b"start":
        sys.exit(1)
    sys.exit(subprocess.Popen(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr).wait())
