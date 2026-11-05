# 从一次代码扫描理解 KernelCoder 的本地 MCP 作业层

整理日期：2026-10-20。本文讲解 MCP 实施计划中已经完成的阶段 1、阶段 2：锁定兼容版本，把 Semgrep 和 Git 接入不依赖 E2B 的本地作业链路。示例用于解释源码，不表示阅读本文时重新执行了扫描。

[第 09 篇](09-e2b-runner-and-workspace.md)沿着一次 Shell 命令讲了远程执行。这一篇换一个入口：

> Agent 刚改完代码，想检查改动、扫描明显的问题，但此时 E2B 还没有启动。

现在它可以直接调用本地 Git 或 Semgrep。KernelCoder 会确定这次检查针对哪一份代码、冻结输入、启动服务、调用工具、保存结果，再回收服务进程。整个过程不需要 E2B key，也不会创建远程沙箱。

这里先把实现范围说清楚：当前 MCP 作业由 `LocalMCPRunner` 管理；已有的 `SandboxRunner` 继续负责 E2B Shell。两者共用任务工作区的约束。本地与 E2B 的 MCP 双后端是后续目标，本轮还没有把 MCP 接进远程 `SandboxRunner`。

## 1. 用两个同名文件看一次扫描

假设任务副本里有两个文件：

```text
left/same.py
right/same.py
```

第一个文件包含：

```python
eval(user_input)
```

第二个文件包含：

```python
print("hello")
```

你让 Agent：

> 检查刚才修改的这两个 Python 文件有没有明显的危险写法。

模型看到的工具和参数很简单：

```json
{
  "name": "mcp__semgrep__scan",
  "arguments": {
    "paths": ["left/same.py", "right/same.py"]
  }
}
```

模型只选择任务内的相对路径。Python 解释器、启动命令、扫描规则、文件内容都由 KernelCoder 确定。

按当前内置规则，这个例子应在 `left/same.py` 命中 `eval(...)`，并保留 `left/`，避免把两个 `same.py` 混为一谈。`right/same.py` 没有命中这条规则；这只说明这次有限的静态检查没有发现对应模式。

调用期间涉及四个位置：

| 位置 | 本次调用中的作用 |
|---|---|
| 用户原项目 | 最终供用户使用的代码；本次扫描不会写回这里 |
| `workspace.tree` | Agent 当前正在修改的任务副本，也是扫描输入的来源 |
| 系统临时目录中的 `input/` | 本次作业冻结的文件副本，交给本地工具使用 |
| 任务目录中的 `mcp-jobs/<job_id>/` | 保存输入清单和运行结果，位于任务代码树之外 |

把这些位置分开，才有办法回答：“这条发现究竟来自哪份代码？”

## 2. 这次具体加了哪些东西

旧的 `mcp.py` 已经能启动 stdio 服务、发现工具，再把工具包装给 Agent。缺少的是任务级的执行管理：任意服务配置不能直接绕过受管理工作区的约束。

这次在它外面补了一层，并增强了底层客户端：

| 文件 | 职责 |
|---|---|
| `kernelcoder/mcp_local.py` | 工具白名单、任务绑定、运行版本检查、冻结输入、本地作业、结果记录 |
| `kernelcoder/mcp.py` | MCP 握手、工具发现、请求响应、超时、结构化结果与协议错误处理 |
| `kernelcoder/mcp_process.py` | 启动并回收服务进程树 |
| `kernelcoder/_mcp_launch.py` | Windows 启动闸门，使服务启动前先进入受管理的进程组 |
| `kernelcoder/mcp_data/profiles.json` | 锁定所用服务的工具输入 schema |
| `kernelcoder/mcp_data/security.yaml` | 第一版固定的两条 Python 扫描规则 |
| `requirements/mcp-local.txt` | 三个直接依赖的精确版本 |
| `requirements/mcp-local-windows-py313.lock.txt` | 已验收的 Windows Python 3.13 环境完整依赖版本 |
| `scripts/lock_mcp_profiles.py` | 更新版本时，从实际服务重新生成 schema 锁 |
| `tests/test_mcp_local.py` | 本地链路、约束、协议、进程清理和真实服务验收 |

此外，`cli.py` 负责启用、恢复配置和延迟启动；`agent.py` 负责准入、权限和串行执行。

扫描的主调用链是：

```text
模型请求 mcp__semgrep__scan
  → Agent 的任务绑定检查与权限检查
  → ManagedMCPTool.execute(paths=...)
  → LocalMCPRunner.run(...)
      → 冻结代码、生成 revision_id、建立作业记录
      → 检查工具环境的实际安装版本
      → MCPClient 启动受管理的服务进程
      → initialize → tools/list → 校验 schema
      → tools/call: semgrep_scan_with_custom_rule
      → 保存结构化结果、回收进程、检查输入版本
  → 把结果摘要交回模型
```

整个过程中没有调用 E2B。

## 3. 启动 KernelCoder 时，为什么不需要先启动 E2B

当前 CLI 的 MCP 后端选项只有：

```text
--mcp-backend off
--mcp-backend local
```

新任务默认 `off`。启用本地链路时，需要指定已经安装锁定依赖的 Python：

```powershell
kernelcoder --mcp-backend local --mcp-python "D:\agent learning\KernelCoder\.tmp\mcp-env\Scripts\python.exe"
```

环境安装方法见 [本地 MCP 使用文档](../docs/mcp-local.md)。

CLI 创建 `MCPManager` 时，只读取工具配置并构造工具对象，不启动 Git MCP 或 Semgrep。只有真正执行工具时，`LocalMCPRunner.run()` 才构造 `MCPClient`，由后者启动服务。

因此，查看 `/mcp` 也不会启动服务。它显示当前任务、Python 路径、锁定版本和本次会话中的最近作业状态。每次调用都有独立进程，没有需要长期维持的 MCP 连接。

同时，普通启动流程去掉了提前检查 E2B 配置的步骤：

```text
创建受管理任务副本
  ├─ 读写文件：操作本地任务副本
  ├─ 调用 Git / Semgrep MCP：启动本地服务
  └─ 调用 Bash：此时才检查 E2B 配置、准备远程执行
```

默认 Shell 后端仍然是 E2B，但创建工作区和 `SandboxRunner` 对象本身不会建立远程连接。显式沙箱检查、需要远程执行的演示以及实际 Bash 调用仍会检查 E2B。

MCP 后端和解释器绝对路径会保存在任务元数据中，恢复任务时继续使用。当前没有 E2B MCP 选项，也没有“远程失败就偷偷改成本地执行”的回退。模型调用仍需要正常的模型配置。

## 4. 什么样的 MCP 工具可以进入受管理任务

`Agent` 没有放开任意 `mcp__...` 工具。

它检查两件事：

1. 工具必须是这次实现的 `ManagedMCPTool`。
2. 工具绑定的工作区必须就是当前 Agent 的工作区。

所以，把另一个任务的 MCP 工具拿过来，或者只给普通工具起一个 MCP 名字，都不能绕过准入检查。旧的任意 MCP 配置入口仍只用于 `unsafe` 模式。

本地受管理 MCP 必须依赖任务副本，所以 `--sandbox unsafe --mcp-backend local` 会被拒绝。

进入工具执行后，还有正常的权限流程。权限键大致长这样：

```text
工具名 @ local : 任务 ID : 配置摘要
```

这个权限摘要包含工具 profiles、解释器绝对路径和规则内容。同名工具换了任务、解释器配置或规则，不能直接继承旧的裸工具名授权。

当前这五个工具都是读取型工具，因此可以进入 KernelCoder 的 plan 模式权限流程，但仍接受普通的授权检查。它们也不会进入普通文件读取工具的并发批次，而是作为串行屏障，在 `workspace.guard()` 下执行。

这把锁协调 KernelCoder 内部的工作区操作；它不能阻止用户在外部编辑器里修改任务副本。因此，作业还会检查前后的文件版本。

## 5. 先确定“哪一份代码”，再启动工具

`LocalMCPRunner.run()` 首先扫描 `workspace.tree`，得到文件清单。每个文件条目包含内容摘要、大小和模式信息。

随后用规范化后的清单生成：

```text
revision_id = "sha256:" + 清单摘要
```

Windows 文件模式会进行归一化，避免把 Windows 权限模拟值当成 Unix 可执行位。

这里有三个不同的 ID：

| ID | 回答的问题 |
|---|---|
| `task_id` | 属于哪一个 KernelCoder 任务？ |
| `job.id` | 属于哪一次 MCP 调用？ |
| `revision_id` | 调用开始时，受管理代码树是什么状态？ |

`revision_id` 是代码树指纹，不是 Git commit hash。扫描只选择两个文件时，它仍标识整个任务代码树；扫描范围另外保存在 `arguments.paths` 中。

复制文件时，`_copy_manifest()` 会再次核对内容与清单是否一致。若文件在扫描清单后、复制完成前发生变化，就拒绝继续使用这份输入。

作业开始时保存：

```text
任务目录/
  mcp-jobs/
    <job_id>/
      input-manifest.json
      result.json
```

`result.json` 起初是 `running`。正常结束或捕获到失败时，再更新状态、耗时、结果和结束时的版本。

清单保存的是指纹信息，临时输入副本会在结束后清理。因此，这一步已经支持追溯“检查的是哪个版本”，尚不提供“从记录恢复那份代码”的能力。

## 6. 为什么同时锁包版本和工具 schema

第一版实际使用的依赖组合是：

| 层次 | 组件 | 锁定值 |
|---|---|---|
| 工具包 | `semgrep` | `1.181.0` |
| 工具包 | `mcp-server-git` | `2026.8.18` |
| 工具环境 SDK | `mcp` | `1.29.0` |
| 通信协议 | MCP wire protocol | `2025-06-18` |

这些数字描述不同的东西：Git MCP 包版本不等于 Git 可执行程序版本，SDK 版本也不等于通信协议版本。

安装验证时，Git MCP `2026.10.19` 要求 MCP SDK `>=2.3,<3`，与 Semgrep 要求的 `1.29.0` 冲突。因此选择了能共用 SDK 1.x 环境的 Git MCP `2026.8.18`。

每次作业会用选定解释器读取三个包的实际安装版本，必须全部匹配。初始化响应中的 `serverInfo.version` 不能替代这个检查：实际服务报告的可能是 SDK 版本。

接下来，客户端完成握手、拉取 `tools/list`，再将本次使用的工具输入 schema 与 `profiles.json` 对比。不匹配就拒绝发送业务调用，避免依赖升级后参数结构已经变化、KernelCoder 却继续按旧约定调用。

`scripts/lock_mcp_profiles.py` 用于有意升级依赖时重新生成锁。生成成功只证明能读取工具定义，仍需要真实扫描和 Git 查询验收。

本轮实际验收环境是 Windows、Python `3.13.7`、Git `2.54.0.windows.1`。Git 可执行程序需要在 PATH 上；当前运行时精确校验的是上表三个 Python 包，并没有把 Git 可执行程序也做成强制版本门槛。

## 7. Semgrep 收到的到底是什么

模型调用的是：

```text
mcp__semgrep__scan(paths=[...])
```

KernelCoder 转换后调用官方服务的：

```text
semgrep_scan_with_custom_rule(code_files=[...], rule="...")
```

其中 `code_files` 来自冻结后的实际文件：

```json
[
  {"path": "left/same.py", "content": "eval(user_input)\n"},
  {"path": "right/same.py", "content": "print(\"hello\")\n"}
]
```

这样选择有两个原因。

第一，文件内容来自任务副本，模型不能提交另一段代码冒充项目文件。路径必须存在于任务清单里，文件必须能按 UTF-8 解码。

第二，实测的普通扫描入口会将路径压缩成 basename。使用带路径和内容的入口，可以保留同名文件所在的目录，后续才能将发现对应到正确文件。

规则由 KernelCoder 从 `security.yaml` 读取，并把规则摘要记入作业。目前只有两条：

| 规则 | 检查内容 |
|---|---|
| `kernelcoder.python-eval` | Python 的 `eval(...)` |
| `kernelcoder.python-shell-true` | Python 的 `subprocess.*(..., shell=True, ...)` |

没有自动拉取远程规则集，也不需要 Semgrep 云账号。当前实现关闭指标发送、版本检查和 MCP tracing。这套规则便于复现和验收，但覆盖范围很小，不能代替项目测试或全面的安全审查。

启动入口也经过实际核验：这里通过已安装的 Semgrep console entrypoint 执行 `mcp`，没有使用已经退役的 `python -m semgrep` 入口。

## 8. 本地进程为什么还要专门管理

Semgrep 服务可能继续启动扫描子进程。只终止最外层 Python，会有扫描引擎残留的风险。

因此，`MCPClient` 的受管理模式使用 `ProcessTree`。Windows 上的流程是：

```text
创建带 kill-on-close 的 Windows Job Object
  → 启动等待闸门的 launcher
  → 把 launcher 加入 Job Object
  → 发送 start 指令
  → launcher 启动真正的 MCP 服务
  → 服务及其子进程随作业一起回收
```

启动闸门解决的是一个时序问题：必须先完成进程归属，再允许服务创建子进程。创建或绑定失败时，调用会失败，不会退回到无人管理的启动方式。

Unix 实现使用独立进程组，结束时回收整个组；本轮真实服务验收在 Windows 上完成，不能据此声称 Linux/E2B 已验收。

每次作业还使用独立的临时 home 和输入目录，并缩减继承的环境变量。模型服务和 E2B 的 key、用户 Git 配置以及 Python 环境注入变量不会照搬给工具。

临时目录放在系统临时目录下，而非深层任务路径中。这同时避开了 Windows Semgrep Unix socket 路径过长的问题，以及输入目录落在原仓库中时可能受到祖先忽略规则影响的问题。

这些措施管理输入、配置和进程生命周期。本地 MCP 仍是以宿主用户权限运行的程序，不具备 E2B 那样的远程隔离边界。

## 9. MCPClient 补强了哪些协议处理

底层仍使用 stdio 上的 JSON-RPC。标准输出承载协议，标准错误单独收集。

这次补强的重点是让一次作业能明确结束：

- 校验初始化返回的协议版本和 tools 能力。
- 支持分页 `tools/list`，拒绝重复游标，并限制页数。
- 保留完整工具结果，包括 `content`、`structuredContent`、`isError`。
- 用请求 ID 匹配响应；超时后清理等待状态，丢弃迟到响应。
- 收到不支持的服务端请求时返回方法不存在错误。
- 限制协议消息长度，并保留有界的 stderr 尾部，便于定位失败。

当前协议版本必须精确匹配 `2025-06-18`，没有实现跨协议版本的自动选择。

作业默认的 60 秒超时用于 `tools/call`。版本检查、初始化、工具发现和 Git 准备各自还有超时，所以它不是从准备到清理都包含在内的 60 秒总期限。

工具正常返回后也会关闭服务。本地链路采用“一次调用，一次启动，一次回收”的方式，避免把前一次作业状态带到下一次调用。

## 10. 返回 completed，究竟证明了什么

`MCPJobResult` 同时保存作业信息和运行结果：

| 字段 | 含义 |
|---|---|
| `job` | 任务、工具、参数、输入版本、profile 摘要、规则摘要和后端 |
| `runtime` | 实际包版本、Python 版本和平台 |
| `result` | 完整 MCP 工具结果 |
| `status` / `detail` | 运行状态与失败说明 |
| `stderr` | 服务诊断输出的尾部 |
| `elapsed_seconds` | 作业耗时 |
| `output_revision_id` | 作业结束时任务代码树的版本 |

常见状态如下：

| 状态 | 含义 |
|---|---|
| `completed` | 工具正常返回，未设置 `isError` |
| `tool_error` | 工具返回了 `isError` |
| `timeout` | MCP 请求在期限内没有响应 |
| `error` | 环境、schema、路径、输入或其他执行错误 |
| `cancelled` | 执行受到键盘中断 |
| `input_changed` | 结束时任务代码树版本与开始时不同 |

其他子进程步骤的超时也可能记为 `error`，具体原因保存在 `detail`。如果最后发现输入版本变化，最终状态会覆盖为 `input_changed`，原始结果仍保留在记录里。

**`completed` 表示调用完成，不表示代码通过验证。** Semgrep 正常返回三个发现，也可能是 `completed`。要判断代码情况，还需要读取扫描内容、错误与跳过文件信息。

传给模型的展示文本超过长度限制会截断，完整 MCP 结果保存在作业记录中。应用崩溃后遗留的 `running` 记录也不能当成完成证据。

本轮建立的是可追溯的工具运行记录；将测试、静态扫描和审查统一归类为 `passed / failed / no_tests`，属于后续验证层。

## 11. Git 工具操作的是哪个仓库

再看一个请求：

> 看看这次任务相对于开始时改了什么。

模型可以调用：

```json
{"name": "mcp__git__git_diff_unstaged", "arguments": {}}
```

模型不能指定 `repo_path`，也不能任意指定历史版本。KernelCoder 会为这次调用创建一个临时 Git 仓库：

```text
创建空仓库
  → 放入任务开始时的 baseline
  → 创建一个 baseline 提交
  → 保留这个 HEAD 和 index
  → 用任务当前文件替换工作树内容
  → 调用 Git MCP 读取状态或差异
  → 清理临时仓库
```

因此：

| Git 概念 | 临时仓库中的对应物 |
|---|---|
| `HEAD` | 任务开始时的 baseline 提交 |
| index | baseline 文件 |
| working tree | 这次调用开始时的任务文件 |
| 未暂存差异 | 当前任务相对于 baseline 的已跟踪文件变化 |

实现还关闭自动换行转换、用户 Git 配置、提交签名与原项目 hooks，并覆盖可能触发内容转换的 attributes。目的在于让比较反映任务文件，而非用户机器上的 Git 配置效果。

原项目的 `.git` 不会复制到临时仓库，也不会在原项目运行 `add` 或 `commit`。用户原本的分支、暂存区以及未提交改动都留在原处。内部创建临时 baseline 提交与“允许模型提交用户仓库”是两件不同的操作。

第一版暴露四个 Git 工具：

| 工具 | 当前实际能看到的内容 |
|---|---|
| `mcp__git__git_status` | 临时任务仓库的状态 |
| `mcp__git__git_diff_unstaged` | 相对 baseline 的已跟踪文件差异 |
| `mcp__git__git_log` | 临时仓库中的一个 baseline 提交 |
| `mcp__git__git_show` | 固定查看 `HEAD`，即 baseline 提交 |

普通 Git diff 不展示未跟踪新文件的完整内容；Git status 对未跟踪文件还受忽略规则影响。因此，这些 Git 输出不能替代完整任务清单。

每次调用都会重建临时仓库。它还不是持久的任务版本库，不提供中间 checkpoint、历史版本选择、恢复或 `last-passed`。临时提交的 hash 也不作为稳定的任务 `revision_id`。

## 12. 对应测试验证了什么

这次验收分成两层。

第一层使用可控的协议服务和临时工作区，检查：

- 注册工具和查看状态不启动服务，不访问 E2B。
- 错误任务绑定、未受管理的 MCP、路径或版本覆盖参数被拒绝。
- 权限绑定到任务与配置，plan 模式仍经过授权流程。
- 环境变量不会直接继承模型和 E2B 凭证。
- 版本或 schema 不匹配会失败，并留下相应记录。
- 超时、取消、分页和不支持的服务端请求得到处理。
- 关闭作业后，服务派生的子进程也被回收。
- Git 视图能处理“原来是文件，后来变成目录”这样的变化。

第二层实际安装锁定的 Git MCP 和 Semgrep：

- 通过 MCP 调用四个 Git 工具。
- 使用一份命中规则、一份不命中的 Python 文件验证扫描。
- 使用不同目录下的同名文件验证结果路径。
- 检查原项目已有的已暂存、未暂存及未跟踪文件状态保持不变。
- 验证整个本地调用过程不依赖 E2B。

上一轮实现验收记录为：全量测试 **338 passed、2 skipped**；后续收尾修改又通过相关定向测试 **19 passed**，以及单独的 CLI 恢复测试 **1 passed**。Ruff 和编译检查也通过。本篇是源码说明，没有把这些历史结果当成撰写本文时重新执行的结果。

真实服务测试需要通过 `KERNELCODER_TEST_MCP_PYTHON` 指定安装环境。没有提供环境时，普通测试通过不能替代真实服务验收。具体运行命令见 [使用文档](../docs/mcp-local.md)。

## 13. 和第 10 篇的三个诉求怎样对应

[第 10 篇](10-verification-versioning-and-review.md)讨论了验证、版本管理和审查。本轮提供的是它们需要的执行基础：

| 方向 | 目前已有 | 后续还需要 |
|---|---|---|
| 验证 | 固定 Semgrep 规则、明确扫描范围、代码版本绑定、结构化作业记录 | 项目测试发现与执行、验证结论分类、证据汇总 |
| 版本管理 | 任务 baseline、当前代码指纹、临时 Git 差异视图 | 持久任务仓库、checkpoint、恢复、`last-passed` |
| 审查 | 可读取任务差异和静态扫描结果 | 独立审查流程、审查结论与被审版本绑定 |
| 执行后端 | 本地 MCP 可独立运行，远程 Shell 延迟启动 | E2B MCP runner、远程桥接与真实远程验收 |

这次最核心的变化是：一次 MCP 调用已经有了明确的任务、输入版本、工具契约、进程生命周期和结果记录。后续增加远程执行或验证工作流时，可以继续沿着这些对象扩展。

## 源码阅读导航

建议先读 `LocalMCPRunner.run()`，把一次作业走通；再读 Git 和 Semgrep 两个分支，最后看协议与进程细节。

以下行号对应整理时的源码；后续文件变化时，以函数名定位为准。

| 位置 | 入口 | 行号 |
|---|---|---|
| `kernelcoder/mcp_local.py` | `revision()`：代码树指纹 | 40 |
| `kernelcoder/mcp_local.py` | `MCPJob` / `MCPJobResult`：作业与结果 | 56 / 69 |
| `kernelcoder/mcp_local.py` | `clean_environment()`：运行环境 | 86 |
| `kernelcoder/mcp_local.py` | `_versions()`：实际安装版本检查 | 118 |
| `kernelcoder/mcp_local.py` | `_git_view()`：临时 baseline 仓库 | 131 |
| `kernelcoder/mcp_local.py` | `LocalMCPRunner.run()`：作业主流程 | 161 |
| `kernelcoder/mcp_local.py` | `ManagedMCPTool`：面向模型的工具接口 | 246 |
| `kernelcoder/mcp_local.py` | `MCPManager`：配置与延迟注册 | 277 |
| `kernelcoder/agent.py` | `_permit()` / `_exec_tool()`：权限与执行 | 169 / 186 |
| `kernelcoder/mcp.py` | `MCPClient`：stdio 客户端 | 40 |
| `kernelcoder/mcp_process.py` | `start()` / `stop()`：进程树生命周期 | 47 / 64 |

配置、安装、复测与升级步骤集中在 [docs/mcp-local.md](../docs/mcp-local.md)。
