# Managed local MCP — stages 1 and 2

Managed MCP operates on a frozen copy of the current task. It does not require
an E2B key, create a remote sandbox, or call the legacy arbitrary-server loader.
Bash remains an E2B operation and checks E2B configuration only when invoked.
Local MCP is a host process, not an OS security sandbox.

## Pinned runtime

Install a separate Python environment, rather than adding scanner dependencies
to KernelCoder's model runtime:

```powershell
python -m venv .tmp/mcp-env
.tmp/mcp-env/Scripts/python.exe -m pip install -r requirements/mcp-local.txt
kernelcoder --mcp-backend local --mcp-python "D:/absolute/path/to/.tmp/mcp-env/Scripts/python.exe"
```

On Unix use `bin/python` instead of `Scripts/python.exe`. Install a Git executable
on PATH too. The tested Windows combination is:

| Component | Version |
| --- | --- |
| Python | 3.13.7 |
| Git executable | 2.54.0.windows.1 |
| Semgrep | 1.181.0 |
| mcp-server-git | 2026.8.18 |
| MCP Python SDK in the tool environment | 1.29.0 |
| MCP wire protocol | 2025-06-18 |

Git MCP 2026.10.19 requires MCP SDK >=2.3 and conflicts with Semgrep's exact
SDK 1.29.0 dependency. The selected Git release resolves that conflict.
`requirements/mcp-local-windows-py313.lock.txt` captures the complete tested
Windows dependency environment; use it instead of the short requirements file
to reproduce all transitive versions. It is not a Linux lock file.

Both services must be installed in the selected environment; each job checks
their versions and actual `tools/list` input schemas before sending a call.
The runtime serverInfo version is the SDK version, so it is not used as the
package version. Services are installed explicitly and never auto-upgraded.

Linux/E2B execution is not validated by the Windows tests. Stage 4 adds the
remote runner and its real acceptance tests.

## Configuration and consent

`--mcp-backend local` opts a managed task into host MCP execution. `off` disables
it. A new task defaults to `off` during this staged rollout. Backend and Python
path are saved in workspace metadata and reused when reopening the task:

```text
kernelcoder --sandbox-session TASK_ID --sandbox-store STORE
```

`--sandbox unsafe --mcp-backend local` is rejected: managed MCP needs a task copy.
Legacy arbitrary MCP configuration remains available only in unsafe mode.
`/mcp` shows task configuration and last-job status without starting a service.
There is no reconnect operation for managed services: each call starts fresh.
E2B MCP selection will be added with the remote runner; there is no host fallback.

The normal consent gate still applies. Grants are scoped to tool, local backend,
task, Python path, profile and rules digest. Old grants for a bare MCP tool name
do not silently authorize the new host execution path. Managed tools are eligible
in KernelCoder plan mode but still require consent; they are serialized under the
workspace guard rather than placed in parallel read batches.

## Tools and scope

* `mcp__git__git_status`: status of the task copy relative to its initial baseline.
* `mcp__git__git_diff_unstaged`: tracked-file changes from that baseline.
* `mcp__git__git_log`: the single initial baseline commit in the disposable view.
* `mcp__git__git_show`: that baseline commit.
* `mcp__semgrep__scan`: accepts `paths`, a nonempty list of task-relative UTF-8
  filenames. Uses official `semgrep_scan_with_custom_rule` with contents taken
  from the frozen task, preserving directory names even for duplicate basenames.

Git views are disposable. They never copy the original `.git`, run original
hooks, inherit user Git configuration, or change the original index or branch.
Persistent task commits, choosing historical revisions and restoring versions
belong to stage 3, and are not claimed by these tools.

The bundled rules currently check Python `eval(...)` and
`subprocess.*(..., shell=True, ...)`. This is a deliberately small reproducible
starting ruleset, not comprehensive security coverage. Rules live in
`kernelcoder/mcp_data/security.yaml`; their hash is included in each scan job.
No registry auto-configuration, cloud authentication, metrics or tracing is
needed. Empty target lists, excluded files and non-UTF-8 files are refused.
Scanner skipped files and errors remain visible in the full returned result.

## Job records and process lifetime

`mcp-jobs/JOB_ID/input-manifest.json` and `result.json` live outside the task tree.
Records include input/output revision, rules/profile digest, actual runtime
versions, structured MCP result, bounded stderr, status and duration.
`completed` means the tool returned normally: it does **not** mean tests passed
or no security findings exist. Verification classification belongs to stage 5.
Only display output is truncated; raw results remain in the bounded job record.
Protocol messages and requests have an 8 MiB limit.

Every call runs in a private OS temporary directory. This avoids inheriting the
project's ignore patterns and keeps Windows Semgrep socket paths short.
Windows uses a gated launcher inside a kill-on-close Job Object; Unix uses a
process group. Timeout/cancellation closes the entire process tree. A job left
`running` after an application crash is not evidence of completion.

## Tests and updating the lock

```powershell
python -m pytest tests/test_mcp.py tests/test_mcp_local.py tests/test_sandbox.py tests/test_plan_mode.py
$env:KERNELCODER_TEST_MCP_PYTHON = (Resolve-Path ".tmp/mcp-env/Scripts/python.exe").Path
python -m pytest tests/test_mcp_local.py -q
```

The real acceptance test uses temporary projects and checks Git status/diff/log/
show, positive and negative Semgrep results, duplicate basenames, preservation
of original staged/unstaged/untracked changes and absence of E2B calls.
Protocol tests cover process-tree cleanup, cancellation, pagination, unsupported
server requests and schema mismatch.

When deliberately updating dependencies, update `PINS` and requirements, install
them into an isolated environment, then regenerate the input schema lock:

```powershell
python scripts/lock_mcp_profiles.py --python .tmp/mcp-env/Scripts/python.exe
```

Run real acceptance before accepting a changed lock. Merely regenerating a schema
does not prove scanning or repository queries work.
