# KernelCoder 学习清单：E2B 执行与工作区

当前已实现本地副本、E2B 执行、双向文件同步和用户审查写回。建议按一条工具调用的顺序阅读。

| 阅读顺序 | 代码入口 | 核心问题 |
|---|---|---|
| 1 | tools/bash.py 的 execute | 模型参数如何进入 Runner？为什么不能开启 unsafe？ |
| 2 | sandbox.py 的 check、run、cleanup、close | 凭据、远程生命周期、结果和恢复如何组织？ |
| 3 | workspace_sync.py 的 upload、download、recover_sync | 增删改怎样同步？为什么要暂存、指纹、备份和日志？ |
| 4 | _sandbox_remote.py 的 execute、stop、scan | 如何限制输出、停止子进程并保护控制文件？ |
| 5 | workspace.py 的 create、guard、path、review、apply | 副本、初始基线、审查候选各是什么？ |
| 6 | agent.py 的 _exec_tool、_exec_tools_parallel | 参数绑定、工作区上下文、连续读取并行与写入串行怎样衔接？ |
| 7 | cli.py 的 main、_sandbox_action、_finish_sandbox | 用户怎样检查后端、恢复失败会话和触发写回？ |

`_sandbox_remote.py` 是复制到 E2B 的独立标准库程序，不在 Windows 宿主执行。它用特权用户保存控制文件，模型命令以专用普通用户运行；E2B 提供宿主与远程之间的隔离。

## 用测试核对理解

- `tests/test_sandbox.py`：副本、越界、链接、审查冲突、写回恢复、子 Agent 继承。
- `tests/test_workspace_parallel.py`：读取并发、修改屏障、线程上下文、权限与中断时的锁保护。
- `tests/test_e2b_runner.py`：使用替代 SDK 的执行、同步失败、日志恢复、环境复用和凭据规则。
- `tests/test_e2b_live.py`：显式 `--run-e2b` 才运行，验证真实远程命令及文件闭环。

学习时依次解释这几个场景：

1. edit_file 改完后，下一次远程测试如何看到最新代码？
2. 命令生成文件后，read_file 为什么可以立即读取？
3. 非零退出为何仍需同步文件？
4. 连接断开为何不能证明命令停止？为什么不能自动重跑？
5. 下载中断和本地提交中断分别如何恢复？
6. 为什么 review 后仍需要在 apply 时检查原项目与候选指纹？

`/undo` 只撤销显式记录的文件编辑检查点，不提供任意 Bash 副作用回滚。工作区锁协调本应用的操作，不锁住用户的编辑器；写回和同步仍需冲突检查。

同轮连续的内建 `read_file`、`glob`、`grep`、`load_skill` 可以并行。主线程持有整个读取批次的工作区锁，工作线程分别继承上下文；文件修改、Bash、子 Agent、任务列表更新和自定义工具按顺序执行。`permissions.py` 的免审批分类与并行读取分类分别处理权限和调度，不能混为一谈。

`/workspace` 同时是文件工具的虚拟路径和远程项目路径；在本机工具侧映射到 tree，并非挂载本机原项目。命令中的 Windows 盘符不会自动改写成远程路径。

后续扩展包括自定义模板、暂停恢复、交互终端和网络策略。它们不影响当前通过用户审查写回的基本链路。操作及恢复说明见 [沙箱指南](README.md)。
