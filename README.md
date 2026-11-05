# KernelCoder

A terminal coding agent with local execution, E2B workspaces, and explicit review before writing sandbox changes back to your project.

[中文](README_CN.md) · [Usage guide](docs/使用手册.md) · [MIT license](LICENSE)

## Features

- Read, search, edit files, and execute shell commands from an interactive CLI or a one-shot prompt.
- Configure an OpenAI-compatible model endpoint, or use the optional LiteLLM backend.
- Choose local execution or an E2B Linux workspace with diff review and explicit writeback.
- Use plan mode, sessions, checkpoints, hooks, skills, and optional MCP tools.
- Configure credentials with `kernelcoder init` and inspect setup offline with `kernelcoder doctor`.

## Install from source

Requires Python 3.11+ and Git. Windows local execution also requires Git Bash, included in Git for Windows.

```powershell
git clone https://github.com/s0Ap224/KernelCoder.git
cd KernelCoder
& .\scripts\install.ps1
kernelcoder --version
```

The Windows installer creates `.venv`, installs the project, and adds its command directory to your user PATH. Reopen other terminals after installation. Keep the repository in place while using this editable installation. If `python` resolves incorrectly, pass `-Python "C:\Path\To\python.exe"` to the installer.

On macOS/Linux:

```bash
git clone https://github.com/s0Ap224/KernelCoder.git
cd KernelCoder
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

This release is distributed through GitHub. Install from this repository; no PyPI release is provided.

## Configure and run

```bash
kernelcoder init --mode local
kernelcoder doctor
cd /path/to/your/project
kernelcoder
```

Enter your provider's model name, API base URL, and API key during setup. Local mode executes tools on your computer; model requests still go to your configured provider. Keys are entered without echo and stored in `~/.kernelcoder/.env`. Settings, history, and sessions use `~/.kernelcoder`.

For E2B execution, run `kernelcoder init --mode e2b` and provide an E2B API key as well. E2B and model services may incur usage charges. Sandbox changes are reviewed and explicitly applied; `--yes` does not authorize writing sandbox changes back to the original project. See [workspace usage](sandbox/README.md).

```bash
kernelcoder -p "Read this project and explain its structure"
kernelcoder --help
```

One-shot mode refuses tools requiring approval by default. Use `--yes` only when you intend to approve tool calls. Local mode operates directly on your project.

Alternatively, copy `.env.example` to `.env` and fill in the values. Configuration precedence is process environment > `--env-file` > project `.env` > user `.env`; explicit model CLI flags override configuration. Legacy project-specific variable names and configuration directories are not automatically migrated.

## Optional tools

Install the LiteLLM backend with `python -m pip install -e ".[litellm]"` and set `KERNELCODER_PROVIDER=litellm`.

For managed MCP tools in E2B task mode, run `kernelcoder setup-mcp`. It installs a separate pinned tool environment. See [MCP setup](docs/mcp-local.md). Skills live in `.kernelcoder/skills` in a project or `~/.kernelcoder/skills` globally; an [example skill](examples/skills/code-review/SKILL.md) is included.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest tests -q
python -m ruff check kernelcoder tests
python -m pip install build twine
python -m build
python -m twine check dist/*
```

The default test suite uses mocks for external services. Live E2B tests require explicit `--run-e2b` and configured credentials. CI runs tests on Windows, macOS, and Linux with Python 3.11–3.13, plus lint and package checks.

## License

MIT. Copyright and permission notices are preserved in [LICENSE](LICENSE). Maintained by [s0Ap224](https://github.com/s0Ap224).
