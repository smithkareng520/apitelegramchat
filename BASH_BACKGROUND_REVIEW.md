# bash 后台运行机制审查报告

审查范围：`src/bash_background.py`（后台 bash 任务：启动 → 句柄 →
查询/停止 → 完成通知），及其与 `src/bash_session.py`、
`src/sandbox.py`、`src/ai/agentic_loops.py` 的接线。

## 整体结论

这一块的设计质量很高，不是随手写的功能：

- **防误杀三保障**讲得很清楚——独立进程组（`start_new_session=True`）
  隔离交互会话的 killpg、启动即返回句柄避免协程取消链误伤、monitor
  用模块级任务 + 防 GC 引用集防止轮次取消误杀后台任务，三条都对应
  真实会出错的场景，不是防御性编程的堆砌。
- **崩溃恢复**（`_ensure_registry_loaded`）用 `.exit` 标记做"终态已落盘"
  与"进程遗孤"的判据，顺序（先写 `.json` 后写 `.exit`）保证了即使
  磁盘写入失败也不会误判；孤儿进程用 `/proc/<pid>/cmdline` 做身份校验，
  防止应用重启后 pid 被内核复用给无关进程时误杀。这是容易被忽略但
  很关键的一处。
- **完成通知"就近搭车"**的机制（终态时入队，下一次真正调用模型的
  请求 drain 并合并为一条尾部 system 消息）对 prompt cache 友好——
  历史前缀不变、通知只出现在尾部，注入点收敛到两个并有专门的不变式
  测试守护，设计上不容易在新增调用路径时被遗漏。
- **幂等守卫**（`_finish_task` 内的终态只记录一次）覆盖了 monitor /
  stop / 重启懒加载三方竞争同一次进程退出的场景，`stop` 路径里"先
  取消 monitor 再发信号"的顺序说明也解释了为什么这个顺序不能颠倒。
- 测试覆盖扎实：生命周期、寿命上限、stop、并发上限、隔离性、重启
  恢复、路由转发都有对应用例，且是真实 spawn 进程验证，不是纯 mock。

## 已修改：已终结任务从不回收，注册表与磁盘无限增长

**位置**：`src/bash_background.py`

**问题**：任务进入终态（done/failed/stopped/expired/lost）后，
`.json`/`.log`/`.exit` 三个磁盘文件会永久保留，内存里的 `_TASKS`
字典条目也不会被移除。虽然"同时运行"的任务数受
`BASH_TASK_MAX_PER_CHAT` 限制，但历史已终结任务不受任何约束——一个
长期运行、反复使用后台任务的 chat，会让：

1. `src/.../tasks/` 目录下的文件数量无限增长；
2. 内存里 `_TASKS[(chat_id, namespace)]` 字典无限增长；
3. `task_action=list` 的输出随之越列越长，模型每次查询都要看一长串
   陈年历史任务。

应用重启后的懒加载（`_ensure_registry_loaded`）会把磁盘上所有历史
`.json` 文件全部读回内存，问题在重启后依然存在，且旧安装积累的历史
文件会在下次重启时被整批载入。

**修改**：新增 `_prune_finished_tasks(chat_id, namespace)`，在
`_finish_task` 记录终态之后（以及注册表懒加载扫描完磁盘之后）自动
触发。只回收终态任务，按 `finished_at` 排序，保留最近
`BASH_TASK_FINISHED_RETENTION`（默认 20，环境变量可调）个，其余的
内存条目与三件磁盘文件一并删除。运行中任务不在回收范围内，删除是
尽力而为（单个文件删除失败不影响其余清理与调用方主流程）。

新增测试 `tests/unit/test_bash_background.py`：
- `test_prune_finished_tasks_keeps_retention_and_running` —— 验证
  超出保留数的最旧任务被回收（内存 + 三件磁盘文件），保留的是最近
  的 N 个而非任意 N 个，运行中任务永不回收；
- `test_finish_task_triggers_prune` —— 验证 `_finish_task` 记录终态
  后自动触发回收，调用方无需手动裁剪。

两个新测试为纯逻辑测试（直接构造 `BackgroundTask`，不真实 spawn
进程），运行快且确定性强。已跑通（含原有 12 个可在当前环境导入的
用例全部通过；另外 4 个因本地缺 `aiohttp`/`cachetools`/`mcp` 等重型
第三方依赖导致 import 链失败，与本次改动无关，非本次改动引入）。

## 未改动：已注意到但认为无需处理的点

- `BASH_TASK_MAX_PER_CHAT` 的运行数检查与 `spawn` 之间存在 await
  窗口，理论上可被并发启动轻微超越——代码注释里已明确说明这是"护栏
  而非硬不变量，可接受"，属于合理取舍，未改动。
- 完成通知队列用 `threading.Lock` 而非纯内存结构：当前 push/drain
  两侧确实都只在同一个 asyncio 事件循环线程内被调用，锁在今天是
  防御性的。但注释说明这是为将来某侧被移入线程（例如请求构建被
  `to_thread` 包装）预留的前瞻设计，临界区极短、不阻塞事件循环，
  成本可忽略，予以保留。
