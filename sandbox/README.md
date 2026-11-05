# E2B Runner：配置、执行与恢复

KernelCoder 默认将 Bash 命令放到 E2B Linux 沙箱运行。文件工具仍操作本地副本，远程变化经过校验回传；原项目只由用户显式 review/apply 写回。`--sandbox unsafe` 是独立的宿主执行模式，没有自动回退。

## 开始使用

安装项目依赖：`python -m pip install -e ".[dev]"`。E2B SDK 固定为 2.56.0。

本机 `.env` 使用 `E2B_API_KEY=...`，也兼容 `E2B_KEY=...`；前者优先。环境文件在后端检查前加载，不覆盖已有环境变量。不要提交密钥。模型 API Key 沿用现有配置；两种密钥都不上传到 E2B。

```powershell
kernelcoder --sandbox-check
kernelcoder --yes -p "读取项目，修改代码并运行测试"
```

`--sandbox-check` 会真实创建沙箱、执行探测命令并销毁，使用 E2B 额度，不需要模型密钥。普通启动只检查配置，第一次 Bash 调用才创建远程沙箱。

## 一次命令的路径

```text
文件工具修改 tree → 上传受管理文件 → E2B 执行
                                      ↓
tree ← 临时下载、校验、日志化更新 ← 确认相关进程停止
  ↓
review 冻结候选 → 用户 apply → 原项目
```

- 同一工作区的 Bash 串行执行并复用沙箱；连续的内建 read_file、glob、grep、load_skill 调用可在工作区锁保护下并行，文件修改及其他状态操作保持顺序。每次 Bash 默认 cwd 为 `/workspace`，显式 cwd 必须指向副本内已有目录，不继承上次 `cd`。
- 项目目录不能是磁盘或文件系统根目录。受管理文件总量上限为 256 MiB（268,435,456 字节），创建副本时先完成容量检查，再复制文件；上传与远程导入沿用同一上限。排除目录不计入，单文件仍限 32 MiB，目录项最多 20,000 个。该限制不是整个会话的磁盘配额：基线、备份和排除范围内的远程依赖另占空间。
- Agent 显式安装依赖，例如 `python -m pip install --user pytest`。依赖和排除目录内的缓存留在远程；恢复本地会话时重建远程环境，需要重新安装。
- 命令在无 sudo 权限的专用用户下执行。仅支持前台、非交互任务；监督程序会终止同一用户遗留的后台子进程。
- 输出在远程按 stdout/stderr 各保留首 6000、尾 3000 字节，SDK 不接收无限命令输出。工具在命令和同步结束后返回结果。
- 非零退出码仍返回 `completed` 和实际 `exit_code`，并同步已经产生的文件。超时和取消需要确认停止后再同步。
- 同步新增、修改、删除及二进制文件；拒绝越界、链接、特殊文件、超限内容和 Windows 不兼容路径。Windows 文件模式按可表达的读写权限规范化。
- `.git`、`.env*`、密钥文件、依赖缓存等按现有规则排除。远程不会获得原项目 Git 历史，项目内的文件名排除规则也不能识别所有敏感内容。
- 网络默认允许，用于安装依赖；系统级安装、自定义模板、交互终端和跨会话暂停恢复不属于本版。

## 审查、写回与资源清理

交互模式使用 `/sandbox status`、`/sandbox diff`、`/sandbox apply`。`--yes` 只影响工具调用同意，不授权写回。

```powershell
kernelcoder --sandbox-session <会话ID> --sandbox-action diff
kernelcoder --sandbox-session <会话ID> --sandbox-action apply
kernelcoder --sandbox-session <会话ID> --sandbox-action cleanup
```

使用自定义存储时补充同一个 `--sandbox-store`。普通无活动会话的 status/diff/apply/discard 不需要 E2B 或模型凭据。正常 CLI 退出会销毁空闲 E2B 实例并保留本地副本；Python API 使用者应在 finally 中调用 `runner.close(workspace)`。

## 失败后恢复

执行状态或下载不确定时保留 `active` 标记，禁止继续操作与写回；不要手动清除。`cleanup` 会取消仍在执行的命令，确认停止，重试下载或恢复已有本地同步日志，然后销毁远程沙箱并解锁。

完整下载后的本地提交日志位于 `sync/<事务ID>/journal.json`，同时保留 candidate 和 backup。提交中断采用向前恢复：仅接受旧指纹或目标指纹，发现额外本地编辑时保持锁定。远程文件校验失败时，先由管理者修复相应远程文件，再重试 cleanup。

若远程环境在下载完成前过期或已被销毁，未下载内容可能无法恢复，不能宣称同步成功；保留会话、日志和原项目，另建会话继续。创建请求断线可能留下尚未拿到 ID 的短时环境，可按控制台 metadata 中的 kernelcoder_workspace 识别；创建时设置的存活时间会限制其生命周期。

旧 Docker 活动标记不能由 E2B 清理代码证明已停止，继续拒绝自动解除。销毁远程资源不会自动解除未恢复的同步状态。

## 验证

```powershell
python -m pytest tests/test_sandbox.py tests/test_e2b_runner.py -q
python -m pytest tests/test_e2b_live.py --run-e2b -q
```

第一条只验证本地契约和 SDK 模拟。第二条显式使用 E2B 额度，只上传小型临时项目，经过真实 Agent 调度验证执行、回传、依赖复用、超时、取消、链接拒绝及清理。默认测试不会调用 E2B。

源码阅读入口见 [学习清单](LEARNING_GUIDE_CN.md)。
