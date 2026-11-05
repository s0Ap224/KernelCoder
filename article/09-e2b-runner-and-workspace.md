# 从一次 Shell 命令理解 SandboxRunner 与工作区

整理日期：2026-10-20。本文记录当前 E2B 实现的阅读讲解；示例用于说明调用链，不表示阅读本文时实际执行了远程命令。

现在的 `SandboxRunner.run()` 是一次远程命令的完整协调入口：准备沙箱、上传文件、执行命令、收集结果、同步文件，最后才把结果交给 Agent。

真正启动 Shell 的代码在远程监督程序里。下面用一个具体例子，沿着调用链走一遍。

## 1. 这次 Agent 要做什么

假设你让 KernelCoder：

> 写一个 Python 脚本，生成 result.txt，然后运行它并检查结果。

Agent 先调用文件工具，写入 `report.py`：

```python
from pathlib import Path

Path("result.txt").write_text("42\n", encoding="utf-8")
print("report ready")
```

接着调用 Bash 工具，参数是：

```python
command = "python report.py"
timeout = 30
cwd = "/workspace"
```

此时有三个不同的位置：

| 位置 | 用途 |
|---|---|
| 原项目目录 | 用户的正式项目，暂时不修改 |
| 本地工作区的 `tree/` | 文件工具读写的项目副本，里面已经有 `report.py` |
| E2B 的 `/workspace` | 远程执行命令时使用的项目目录 |

这里的 `workspace` 参数，是管理本地副本、状态和审查记录的 Python 对象；E2B 沙箱是另外一个对象。

整体链路是：

```text
Agent 生成工具调用
  → BashTool.execute()
  → SandboxRunner.run()
      → 本地 tree 上传到 E2B /workspace
      → 远程执行 python report.py
      → 获取输出、退出码
      → result.txt 下载回本地 tree
  → 工具结果返回 Agent
  → Agent 用文件工具读取本地 tree/result.txt

用户之后 review / apply
  → 才将审查通过的变化写回原项目
```

## 2. 进入 run() 之前，谁调用了谁

先看 `kernelcoder/agent.py` 的 `Agent._exec_tool()`：

```text
Agent._exec_tool()
    → 找到 BashTool
    → 检查参数能否绑定到 execute()
    → 进入工作区 guard()
    → BashTool.execute(...)
```

`kernelcoder/tools/bash.py` 的 `BashTool.execute()` 获取当前工作区，做危险命令检查，然后调用：

```python
result = self.runner.run(workspace, command, timeout, cwd)
```

三者的职责是：

- `Agent`：决定调用哪个工具，统一调度。
- `BashTool`：提供模型能调用的参数接口，并检查危险命令。
- `SandboxRunner`：完成这次远程执行和文件同步。

参数绑定只检查调用方式是否匹配函数签名；它不等于运行时类型校验，也不提供执行隔离。危险命令匹配同样只是前置策略检查，不能代替沙箱。

下面进入 `kernelcoder/sandbox.py` 的 `SandboxRunner.run()`。

## 3. 检查参数，然后取得工作区的使用权

开头检查：

```python
# command 必须是非空字符串，不能包含 NUL
# timeout 必须是 1～600 的整数
# cwd 必须是字符串或 None
# 拒绝明显需要交互的命令
```

我们的 `python report.py` 带有脚本参数，可以通过。裸 `python` 会进入交互解释器，因此会被拒绝。

检查失败，直接返回：

```python
ExecutionResult("policy_denied", detail="...")
```

检查通过后：

```python
with workspace.guard():
```

`guard()` 就是给工作区操作加一层保护：

1. **加锁**：防止其他线程或进程同时操作这个工作区；同一线程允许嵌套进入。
2. **检查状态**：确认工作区可用，没有上次遗留的执行或同步。
3. **设置上下文**：让文件工具知道当前应该访问哪个工作区副本。
4. **执行代码**：通过 `yield` 运行 `with` 中的上传、执行、下载等操作。
5. **退出清理**：无论正常返回还是异常，都恢复上下文并释放锁。

这里有两个作用：

1. 同一工作区的操作互相协调，避免文件工具与 Bash 同时修改副本。
2. 检查上一次执行或同步是否还有未处理状态；有的话拒绝继续。

`guard()` 还会设置 `CURRENT_WORKSPACE`，让内部文件工具知道当前使用哪个工作区。Agent 外层已经进入过一次，这里允许同一调用链重入。

接下来验证目录：

```python
directory = workspace.path(cwd or "/workspace")
```

这里先得到的是本地 `tree` 内的路径：

```text
cwd="/workspace"
    → 本地 tree/

cwd="/workspace/src"
    → 本地 tree/src/
```

系统先确认它属于工作区并且确实是目录，随后才把相对路径传给远程。每次默认从 `/workspace` 开始，不会继承上一条命令中的 `cd`。

## 4. 检查配置，创建或复用 E2B 沙箱

```python
self.check()
sandbox = self._ensure(workspace, timeout)
```

两者分工不同。

`check()` 加载 `.env`、取得 `E2B_API_KEY` 或兼容的 `E2B_KEY`，并检查 SDK 是否可用。它本身不证明网络连接成功。

`_ensure()` 才负责实际沙箱：

- 当前工作区已有可用的会话沙箱：复用并延长生命周期。
- 没有：创建 E2B 沙箱，准备 `/workspace`，上传远程监督程序，创建运行用户。

因此：

```text
第一次 Bash：创建沙箱 → 执行
第二次 Bash：复用沙箱 → 执行
```

这就是同一会话里第一次 `pip install` 后，第二次命令还能使用已安装依赖的基础。沙箱允许联网，但不会把宿主环境变量整包传过去。API 密钥留在宿主 SDK 客户端。

本地会话恢复不等于恢复原来的远程运行环境；当前设计重新创建沙箱并上传文件，依赖需要重新安装。

## 5. 记录执行状态，把本地文件上传

先为这次命令创建独立 `job`：

```python
job = uuid.uuid4().hex
active = {
    "backend": "e2b",
    "sandbox_id": sandbox.sandbox_id,
    "job": job,
    "phase": "uploading",
}
workspace.meta["active"] = active
workspace._save()
```

三个 ID 不要混淆：

| ID | 标识什么 |
|---|---|
| `workspace.id` | 本地项目副本 |
| `sandbox.sandbox_id` | 当前远程运行环境 |
| `job` | 本次命令执行 |

`active` 是落盘的状态记录，不是操作系统锁。程序如果在执行中断开，下次打开工作区仍然能知道“上次操作尚未安全结束”。

然后：

```python
sync = WorkspaceSync(workspace, sandbox, job)

before = sync.upload(
    command,
    timeout,
    directory.relative_to(workspace.tree).as_posix(),
)
```

`kernelcoder/workspace_sync.py` 的 `upload()` 会：

1. 扫描本地 `tree` 中受管理的文件。
2. 上传文件内容，以及包含命令、超时、目录和文件清单的 `request.json`。
3. 让远程程序把文件变化应用到 `/workspace`。
4. 检查上传期间本地文件没有发生变化。

于是：

```text
本地 tree/report.py
    → E2B /workspace/report.py
```

远程受管理文件的新增、修改、删除会同步；排除范围里的依赖和缓存保留。目前实现会上传全部受管理文件内容，远程再根据指纹判断哪些需要更新。

这里的 `before` 是本次命令执行前本地 `tree` 的文件指纹，不是创建工作区时的原项目基线。

## 6. 远程真正启动 Bash

上传完成后：

```python
active.update(phase="executing", before=before)
workspace._save()

invoke(sandbox, "run", job, timeout=timeout + 30)
```

先保存 `executing`，再发送执行请求。这样即使请求发出后连接断了，也不会把这次命令误认为“没有执行过”。

调用中还有一层：

```text
invoke()
  → E2B SDK commands.run()
  → 运行远程 supervisor.py run <job>
  → supervisor 读取 request.json
  → supervisor 启动真正的用户命令
```

SDK 运行的是监督程序。你的 `python report.py` 由监督程序启动。

`kernelcoder/_sandbox_remote.py` 的 `execute()` 中，核心代码是：

```python
proc = subprocess.Popen(
    ["/bin/bash", "-c", request["command"]],
    cwd=cwd,
    env=env,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
    preexec_fn=demote,
)
```

对应我们的例子，相当于在 E2B 内部运行：

```bash
cd /workspace
/bin/bash -c "python report.py"
```

监督程序用普通 `kernelcoder` 用户运行命令，并负责：

- 持续读取 stdout、stderr，只保留有限长度的首尾内容。
- 监控超时与取消请求。
- 命令结束后停止该执行用户的残留进程。
- 确认停止后，把结果写入私有目录中的 `result.json`。

这比原来的 `run_shell(..., capture_output=True)` 多了一层执行管理。输出限制发生在远程读取阶段，避免先让 SDK 收集无限输出再截断。

监督程序与控制文件位于普通执行用户不能写入的私有目录；E2B 提供远程运行环境与宿主之间的隔离。

这次命令产生两类东西：

| 内容 | 去向 |
|---|---|
| `print("report ready")` | stdout → 执行结果 |
| `result.txt` | 远程 `/workspace/result.txt` → 后续文件同步 |

## 7. 拿到执行结果，再把文件同步回来

远程结束后，Runner 读取结果文件。下面省略了实际控制目录路径：

```python
result = ExecutionResult(
    **json.loads(read_remote(sandbox, ".../result.json", 128_000))
)
```

结果相当于：

```python
ExecutionResult(
    status="completed",
    output="report ready\n",
    exit_code=0,
    detail="...",
)
```

`**` 把 JSON 字典展开成关键字参数，因此字典的 `status` 对应 `ExecutionResult.status`，BashTool 随后访问的 `result.status` 就是这个字段。

这时还不能直接 `return result`，因为 `result.txt` 仍在远程。

所以接着：

```python
active["phase"] = "syncing"
workspace._save()

sync.download(before)
```

`download()` 的处理顺序是：

```text
扫描远程文件
  → 校验路径、类型、大小、数量
  → 下载到本地临时候选区
  → 验证内容指纹与本地文件名兼容性
  → 检查本地 tree 仍与 before 一致
  → 保存备份和恢复日志
  → 将候选内容应用到 tree
```

为什么还要检查 `before`？因为工作区锁只能协调 KernelCoder 自己的操作。用户或其他程序仍可能在命令运行期间修改本地 `tree`，系统不能悄悄覆盖这些修改。

同步通过日志支持中断后的继续恢复，并不意味着整个目录更新是一个不可分割的文件系统事务。

同步完成后：

```text
本地 tree/
├── report.py
└── result.txt       ← 现在 Agent 可以读取它
```

原项目仍然没有因此改变。

最后将状态提交、清除 `active`，清理本次任务的远程临时文件，再返回结果。远程沙箱继续保留，供下一次 Bash 调用复用。

BashTool 执行 `result.render()`，Agent 把文本结果加入工具消息，然后继续下一轮推理；它现在可以调用文件读取工具检查 `result.txt`。

## 8. 失败时为什么不能简单清掉状态

正常状态链是：

```text
uploading → executing → syncing → committed → 清除 active
```

失败处理要区分“命令失败”和“执行管理失败”：

| 情况 | 当前行为 |
|---|---|
| Python 报错，退出码为 1 | 仍同步已产生的文件；返回 `completed`，`exit_code=1` |
| 超时或取消 | 先确认相关进程停止，再同步；成功完成这些步骤后返回对应状态 |
| 通信中断，无法确认执行情况 | 保留活动状态，不自动重跑命令 |
| 文件同步失败 | 返回 `sync_error`，保留恢复信息，阻止继续操作 |

`completed` 表示“命令已经结束，并完成同步”，不等于命令业务成功。如果终止确认或后续同步失败，也不能只根据发生过超时或 Ctrl+C 就报告一个已经安全结束的状态。

Runner 返回 `cancelled` 时，BashTool 将其转换成 `KeyboardInterrupt`，让 Agent 和 CLI 进入中断处理。

正常会话退出时，`close()` 销毁空闲远程沙箱，本地工作区保留。如果仍有未处理执行或同步，则需要 `cleanup()` 处理，不能只清掉标记。旧版无法识别、无法确认停止的活动标记也不能自动解除。

## 9. workspace 对 KernelCoder 的运行方式改变了什么

最大的改变是：KernelCoder 从“直接操作用户项目”，变成“围绕一个受管理副本工作，最后审查写回”。

| 方面 | 直接宿主运行方式 | 当前工作区方式 |
|---|---|---|
| 文件工具 | 直接访问宿主路径 | 统一访问本地 `tree` |
| Bash | 宿主启动进程 | E2B 执行，前后同步文件 |
| 路径 | 由宿主目录决定 | `/workspace` 统一映射到工作区 |
| 原项目修改 | 操作后立即发生 | 用户 `review/apply` 后发生 |
| 多工具调用 | 可以并发执行 | 连续读取并行，状态修改按顺序执行 |
| 异常恢复 | 主要看单次工具报错 | 还检查执行阶段、同步日志 |
| 子 Agent | 容易形成独立操作范围 | 继承同一个工作区和策略 |

工作区建立时，会为受管理的文件保存初始基线，并建立可编辑的 `tree`。文件工具通过 `CURRENT_WORKSPACE` 和 `tool_path()` 统一访问副本；Bash 通过上传和下载与同一副本保持一致。

同轮并行的规则是：连续的内建 `read_file`、`glob`、`grep`、`load_skill` 调用组成读取批次，最多使用 8 个线程；`write_file`、`edit_file`、`bash`、`agent`、`todo_write` 以及其他工具作为串行边界。例如：

```text
read A、grep B、write C、read C、glob D、bash
    → 并行(read A, grep B)
    → write C
    → 并行(read C, glob D)
    → bash
```

主线程为整个读取批次持有一次工作区锁，每个工作线程获得独立的上下文副本，因此仍使用同一个 `CURRENT_WORKSPACE`。读取线程不再次获取主线程持有的锁；全部读取结束后才允许后续修改。结果按模型原调用顺序返回，权限提示仍在主线程处理。

`Permission.READ_ONLY` 表示免审批，不等于可以并行：`todo_write` 会替换共享任务列表，因此仍串行。并行调度另外核对工具名和内建实现，同名自定义工具不会自动取得并行资格。中断时取消排队任务，等待已运行的读取结束后才释放锁。

这项优化针对本地读取批次，不改变单次 Bash 的 E2B 上传、执行和下载流程。

还有几处容易忽略：

- 沙箱模式关闭宿主 Hooks，并拒绝未受管理的 MCP 工具；现在允许注册绑定当前任务工作区的本地 Semgrep、Git 工具。其输入冻结、权限和进程管理见[第 11 篇](11-local-mcp-jobs.md)。
- `checkpoint/undo` 主要服务文件工具的编辑撤销，不代表任意 Bash 操作都能完整回滚。
- 显式 `unsafe` 模式仍保留宿主执行分支；E2B 不可用不会自动回退到这个分支。
- 第一版不保留后台常驻服务，也不支持交互终端。

## 10. 分清两种同步，以及两份“执行前状态”

第一种同步：

```text
E2B /workspace → 本地 tree
```

这是 `run()` 自动完成的，目的是让 Agent 接着工作。

第二种同步：

```text
本地 tree → 原项目
```

这是用户通过 `workspace.review()` 和 `workspace.apply()` 触发的。系统冻结审查候选、检查原项目冲突后，才进行写回。`--yes` 不等于授权写回。

对应地，需要区分两份状态：

| 状态 | 记录时间 | 主要用途 |
|---|---|---|
| 工作区初始基线 | 创建工作区时 | 判断最终改了什么，检查原项目写回冲突 |
| `run()` 中的 `before` | 每次命令上传时 | 判断远程运行期间本地副本是否又被修改 |

一条 Bash 命令正常返回时，Agent 看到的输出与本地副本已经完成对应的同步；用户正式项目是否接受这些变化，仍由后面的审查写回来决定。

## 源码阅读定位

以下行号对应本文整理时的版本，后续代码修改可能使行号变化；优先按函数名定位。

| 文件 | 入口 | 当时行号 |
|---|---|---|
| `kernelcoder/agent.py` | `_exec_tool` / `_exec_tools_parallel` | 167 / 187 |
| `kernelcoder/tools/bash.py` | `BashTool.execute` | 93 |
| `kernelcoder/sandbox.py` | `_ensure` / `run` / `close` | 90 / 162 / 256 |
| `kernelcoder/workspace_sync.py` | `invoke` / `upload` / `download` | 30 / 89 / 107 |
| `kernelcoder/_sandbox_remote.py` | `execute` | 183 |
| `kernelcoder/workspace.py` | `guard` / `path` | 161 / 237 |
| `kernelcoder/workspace.py` | `review` / `apply` / `tool_path` | 265 / 313 / 418 |
