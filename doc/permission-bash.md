# KernelCoder Permission 与 Bash Command 审批

Permission 负责把一次工具调用判定为 `allow`、`deny` 或 `ask`。Agent 在实际执行前完成参数准备与权限判定，CLI 接收 `ask` 并展示完整调用，Bash 工具使用审批时确定的命令、目录和 Shell 执行。规则、权限模式和用户批准共同决定调用是否可以继续。

本文对应 [permissions.py](../kernelcoder/permissions.py)、[bash_policy.py](../kernelcoder/bash_policy.py)、[Agent](../kernelcoder/agent.py) 和 [BashTool](../kernelcoder/tools/bash.py)。改造前版本保存在 Git 分支 `permission-v0.1.0`。

## 1. 整条链路

```text
模型发起工具调用
  ↓
Agent._permit：查找工具、绑定参数、准备实际命令和 cwd
  ↓
Permission.evaluate
  ├─ Plan 限制、工具硬限制、deny 规则 → deny
  ├─ 显式 ask 规则、需要确认的内容风险 → ask
  ├─ allow 规则、可信工具默认许可、模式许可 → allow
  └─ 其余 → ask
  ↓
Permission.authorize
  ├─ deny → 返回拒绝文本
  ├─ allow → 继续执行
  └─ ask
      ├─ 无审批回调 → 拒绝
      └─ CLI 展示完整内容
          ├─ 拒绝、EOF、Ctrl+C → 拒绝
          ├─ 允许本次 → 执行
          └─ 可复用的普通调用 → 保存明确规则，再执行
  ↓
工作区保护与工具执行
  ↓
结果写回工具消息，Agent 继续下一轮
```

同一轮的权限检查和审批在调度线程完成。工作区中连续的可信读取仍可并行，修改与 Bash 作为串行屏障。批准不会跳过工作区锁、活动状态和路径检查。子 Agent 共享父级 Permission，并继承 Plan 状态。

## 2. 修改前后的区别

| 项目 | `permission-v0.1.0` | 当前版本 |
| --- | --- | --- |
| 判定结果 | `None` 表示放行，字符串表示拒绝，询问混在检查中 | `evaluate()` 返回三态与原因，`authorize()` 处理审批 |
| 放行依据 | 工具名在只读集合、`allow_all` 或 `_always` 中 | 先检查限制，再匹配规则、可信实现和模式 |
| Bash 永久授权 | `always` 可以全局放行整个 Bash | CLI 默认保存精确命令，并绑定项目、cwd 和执行后端 |
| 审批展示 | `_brief()` 截断参数 | 完整参数、执行位置、原因和拟保存规则 |
| 危险检查 | 用户批准后在 Bash 中执行正则检查 | 审批前内容分析，执行入口复用硬拒绝检查 |
| 组合命令 | 没有命令级授权分析 | 每个组成命令都需要许可，未知结构要求确认 |
| 无权限对象 | Agent 隐式放行 | Agent 建立默认 Permission |
| 无人审批 | 修改类工具拒绝 | 任意 `ask` 都拒绝，明确 allow 可以执行 |

原有 `_split_statements()` 继续服务于宿主模式的 cwd 跟踪。权限分析使用独立的 `analyze_command()`，两者分别承担目录记忆与审批判断。

## 3. 权限对象与顺序

| 接口 | 职责 | 返回值 |
| --- | --- | --- |
| `Permission.evaluate(tool, arguments, context=...)` | 检查限制、规则和模式，不调用 UI | `PermissionDecision` |
| `Permission.authorize(...)` | 处理三态，调用审批回调，按选择保存授权 | `None` 或拒绝文本 |
| `Permission.check(name, arguments)` | 为旧调用方保留的兼容入口 | `None` 或拒绝文本 |
| `Permission.describe()` | 展示项目、模式和规则 | JSON 文本 |
| `Permission.reset()` | 清除当前项目保存授权与当前会话授权 | 无返回值 |

`PermissionDecision` 包含 `behavior`、`reason`、`source`、`matched_rule` 和 `reusable`。`reusable` 只有在没有既有授权、命令可可靠分析时成立。显式 ask、敏感路径、脚本执行与复杂语法的批准只对当前调用有效。

`deny` 与 `ask` 分别扫描全部规则，配置文件的排列顺序不会让 allow 越过它们。Plan 限制和硬拒绝先检查。可信内建工具通过实际实现类型判断，自定义工具使用同名不会自动取得默认许可。

```python
permission = Permission(rules=[
    PermissionRule("allow", "bash", "git status"),
    PermissionRule("ask", "bash", "npm publish", match="prefix"),
    PermissionRule("deny", "bash", "git push", match="prefix"),
])
```

`content=None` 表示整工具规则。`exact` 支持完整命令精确匹配与组成命令匹配，`prefix` 比较解析后的命令词边界。`git status` 前缀不会匹配 `git statusx`。CLI 自动生成精确规则，前缀或整工具规则由用户明确配置。

## 4. Bash 命令准备与分析

`BashTool.prepare()` 校验命令、timeout 和 cwd。命令必须是无 NUL 的非空字符串，timeout 为 1 到 600 的整数。宿主模式把 cwd 固定为绝对目录，E2B 模式通过工作区映射到 `/workspace`。

宿主的 `prepare_shell()` 确定 Shell 路径和方言。在 Windows 找到 Git Bash 时，先转换命令中的 Windows 路径，再把最终命令交给审批。执行时传入相同 `PreparedShell`，命令与准备结果不同会拒绝执行。CMD 使用整条询问，不使用 POSIX 规则自动放行。

`analyze_command()` 用词法分析识别简单命令和基础连接符。每个命令有独立的单词列表，`&&`、`||`、分号、管道与换行把调用分成多个部分。整条调用只执行一次，不把分析出的部分逐条重跑。

| 输入 | 处理 |
| --- | --- |
| `git status` | 可匹配精确规则，也可生成精确授权 |
| `git status && git diff` | 两个部分都获准才能自动执行 |
| `git status; npm publish` | status 的许可不能覆盖 publish |
| `git status > report.txt` | 重定向触发整条确认 |
| `echo "$(command)"` | 命令替换触发整条确认 |
| `python script.py`、`bash -c ...` | 脚本或嵌套执行触发确认 |
| `rm -rf build` | 删除操作需要确认 |
| `rm -rf /`、明确的格式化或原始设备写入、典型 fork bomb | 硬拒绝 |

当前分析器是保守的词法检查。扩展语法、转义、通配符或无法解析的输入都可能触发询问。它不能证明任意脚本的行为安全，宿主执行仍依赖用户对项目和命令的信任。

## 5. 模式、审批与保存

| 权限模式 | 行为 |
| --- | --- |
| `default` | 可信内建读取和 Todo 更新默认放行，其余按规则或询问 |
| `acceptEdits` | 当前项目内普通文件的内建编辑默认放行，敏感或外部路径询问 |
| `bypass` | 放行普通未授权操作，显式 ask、内容风险和 deny 继续生效 |

`--yes` 是 bypass 的兼容选项。执行位置用 `--mode local` 或 `--mode e2b` 选择，权限模式用 `--permission-mode` 选择。已有 `/plan` 继续限制修改操作，输入 `approve` 或 `/plan` 退出后才恢复执行。

```text
kernelcoder --mode local --permission-mode acceptEdits
kernelcoder --mode local --yes
/permissions
/permissions reset
```

普通 Bash 审批提供 `[y]` 本次、`[s]` 会话、`[p]` 项目保存和 `[n]` 拒绝。需要每次确认的调用只提供本次允许与拒绝。文件编辑可通过 acceptEdits 减少重复询问。

宿主的 `~/.kernelcoder/permissions.json` 使用 version 2 格式，以规范化原项目路径分组。E2B 从工作区的 original 路径确定项目身份。CLI 保存的 Bash 规则附带 cwd 与 execution，切换目录或后端后需要重新取得许可。

旧版 `always` 数据保留为 legacy，但不自动产生新授权。损坏配置会报错。保存使用进程文件锁、旧内容检查和临时文件替换，避免并发 CLI 覆盖彼此的权限文件。保存失败时告知用户授权只在当前会话有效。其他进程修改权限文件后，已有会话需要重新启动才能载入最新规则。

## 6. 测试与阅读顺序

先读 `PermissionDecision`、`evaluate()` 和 `authorize()`，再读 `BashTool.prepare()`、`analyze_command()` 与 Agent 的 `_permit()`。最后查看 CLI 审批回调和项目存储，即可串起一次命令从模型请求到执行的过程。

`test_bash_policy.py` 覆盖规则优先级、组合命令、复杂语法、项目隔离、旧权限迁移、可信实现、完整审批展示以及准备结果与执行结果的一致性。`test_permissions.py` 验证同意、拒绝、保存和非交互行为。已有 Plan、子 Agent、并行读取、Hooks、工作区和 E2B 假客户端测试共同检查调用链。

可以通过 `git diff permission-v0.1.0..main -- kernelcoder/permissions.py kernelcoder/tools/bash.py` 对照核心变化。真实 E2B 创建与网络验收仍通过已有 `--run-e2b` 测试入口单独运行。
