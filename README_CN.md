# KernelCoder

终端编程助手，支持本地执行、E2B 工作区，以及审查差异后显式写回项目。

[English](README.md) · [完整使用手册](docs/使用手册.md) · [MIT 许可证](LICENSE)

## 功能

- 在交互式终端或单次任务中读取、搜索、编辑文件和执行命令。
- 连接 OpenAI 兼容模型接口，或选用 LiteLLM 后端。
- 选择本地执行或 E2B Linux 工作区，支持差异审查和显式写回。
- 支持计划模式、会话、检查点、hooks、技能和可选 MCP 工具。
- 使用 `kernelcoder init` 完成配置，使用 `kernelcoder doctor` 离线检查环境。

## 从源码安装

需要 Python 3.11+ 和 Git。Windows 本地执行还需要 Git for Windows 自带的 Git Bash。

```powershell
git clone https://github.com/s0Ap224/KernelCoder.git
cd KernelCoder
& .\scripts\install.ps1
kernelcoder --version
```

安装脚本创建 `.venv`、安装项目，并把命令目录加入用户 PATH；其他已打开的终端需重新打开。可编辑安装期间，请保留仓库所在位置。如果 `python` 指向错误解释器，可向脚本传入 `-Python "C:\Python313\python.exe"`。

macOS/Linux：

```bash
git clone https://github.com/s0Ap224/KernelCoder.git
cd KernelCoder
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

本版通过 GitHub 提供源码，请从本仓库安装；尚未发布到 PyPI。

## 配置并启动

```powershell
kernelcoder init --mode local
kernelcoder doctor
cd "C:\Projects\你的项目"
kernelcoder
```

按提示填写服务商的模型名、API Base URL 和 API Key。密钥输入不回显，保存在 `~/.kernelcoder/.env`；设置、历史和会话使用 `~/.kernelcoder`。本地模式在本机执行工具，模型请求仍发往你配置的服务。

使用 E2B 时，运行 `kernelcoder init --mode e2b`，并额外填写 E2B API Key。模型和 E2B 服务可能产生使用费用。E2B 工作区修改需经过差异审查并显式写回；`--yes` 不授权写回原项目。详见 [工作区说明](sandbox/README.md)。

```powershell
kernelcoder -p "阅读当前项目并说明结构"
kernelcoder --help
```

单次任务默认拒绝需要授权的工具调用；只有打算允许这些调用时才使用 `--yes`。本地模式直接操作当前项目。

也可以将 `.env.example` 复制为 `.env` 后填写配置。配置优先级为：进程环境变量 > `--env-file` > 项目 `.env` > 用户 `.env`；模型相关命令行参数优先于配置。本版不自动迁移旧项目专用变量名和配置目录。

## 可选能力

安装 LiteLLM 后端：`python -m pip install -e ".[litellm]"`，并设置 `KERNELCODER_PROVIDER=litellm`。

在 E2B 任务模式中使用受管 MCP 工具：运行 `kernelcoder setup-mcp`，安装独立的固定版本工具环境，详见 [MCP 文档](docs/mcp-local.md)。项目技能放在 `.kernelcoder/skills`，全局技能放在 `~/.kernelcoder/skills`，可参考[技能示例](examples/skills/code-review/SKILL.md)。

## 开发与验证

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests -q
python -m ruff check kernelcoder tests
python -m pip install build twine
python -m build
python -m twine check dist/*
```

默认测试使用外部服务替身；真实 E2B 测试需要显式添加 `--run-e2b` 并配置凭据。CI 覆盖 Windows、macOS、Linux 与 Python 3.11–3.13，同时执行代码检查和构建检查。

## 许可证

采用 MIT 许可证，版权及许可声明保留在 [LICENSE](LICENSE) 中。维护者：[s0Ap224](https://github.com/s0Ap224)。


## 工具权限与 Bash 审批

使用 `--permission-mode default|acceptEdits|bypass` 选择权限模式。`--yes` 等价于 bypass，显式 ask、deny 和内容风险检查仍生效。审批展示完整命令、cwd 和执行后端，可将普通命令的精确授权保存到会话或当前项目。`/permissions` 查看规则，`/permissions reset` 清除当前项目授权。旧版全局 always 授权需要重新确认。

实现流程与修改前后对比见 [Permission 与 Bash Command 审批](doc/permission-bash.md)。
