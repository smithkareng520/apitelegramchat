# 摄取通道失聪事故与修复（2026-09-15）

## 现象

发新消息完全没有响应，但服务看起来一切正常：

```
{"level":"INFO","name":"app","message":"[unknown] heartbeat: loop_lag=0.00s queue=0/1000 active_tasks=0 tasks=12"}
[2026-09-15 11:43:50 +0800] [1] [INFO] 10.235.25.167:42132 GET /health 1.1 200 16 847
```

- 进程活着，event loop 完全健康（`loop_lag=0.00s`，心跳每分钟准点）；
- `/health` 恒 200，Render 与 Docker HEALTHCHECK 全绿，实例永远不重建；
- `queue=0/1000 active_tasks=0 tasks=12` **连续 7 分钟一个字节都没变**；
- 全程**没有任何一行** `telegram polling fetched` / `telegram polling queued
  update_id` / `telegram worker processing update`。

最后一次真实活动是一轮 TIMER 主动唤醒发出长文，之后只剩心跳。

## 结论

不是"没人发消息"，是**摄取通道（getUpdates 长轮询）已经死了，而系统里没有
任何人知道**。update 根本没进到进程里，所以业务链路一行日志都没有。

一条常被误判的线索：日志开头那段带 `<h2>` / `<a name>` / `<tg-reference>`
的 HTML **不是报错**，而是 `core/telegram_messaging.py` 在 POST 之前打的
`sendRichMessage payload HTML` INFO 日志。发送层本身已经三层兜底（媒体逐个
降级 → 全部降级 → 纯文本段落），失败只返回 `False`，不会抛异常，更不会杀死
任何循环。

## 根因（三个缺陷叠加）

1. **轮询循环把任意取消当成关停。**
   `poll_updates_forever` 里 `except asyncio.CancelledError: raise`。但取消
   信号不只来自关停：aiohttp 的 `ClientTimeout` 本身就是靠取消请求任务实现
   的，取消落在 `async with` 退出 / 连接归还的窗口里时会以 `CancelledError`
   （而非 `TimeoutError`）逸出。**一次就够，进程永久失聪。**
   `telegram_worker` 同病：`await asyncio.create_task(process_update(...))`
   ——子任务被打断机制（打断旧回合 / 媒体组重排 / proactive 打断）取消时，
   `CancelledError` 沿 await 链回传，worker 静默死亡，队列只进不出。

2. **没有任何主管。** `_telegram_polling_task` 在 `before_serving` 创建后，
   除了关停路径再没人看过它一眼，`.done()` 从来没被检查过。

3. **`/health` 是写死的 200。** 最致命的状态（进程活着但收不到消息）对外
   表现为完全健康，平台永远不会重启，故障只能靠人肉发现。

附带缺陷：`_fetch_updates` 文档写"异常返回 None"但**没有任何 try/except**；
心跳不含摄取通道状态，日志里"空闲"和"失聪"长得一模一样。

## 修复

### `src/telegram_polling.py`（重写）

- **循环自愈**：非关停来源的 `CancelledError` 记 CRITICAL 后 `uncancel()`
  并继续轮询。关停 / 主管重启会先置 `_stop_requested`，退出语义不被稀释。
- **主管任务 `_supervise`**：子任务无论以何种方式结束（异常 / 被取消 /
  意外 return）都按指数退避重新拉起；同时监测**停摆**——连续
  `STALL_SECONDS`（默认 `(POLL_TIMEOUT+15)*3` = 120s）没有一次成功的
  getUpdates（空轮询也算成功）即强制重启，覆盖"请求挂死、连接池饿死"这类
  不抛异常的静默故障。
- `_fetch_updates` 兑现契约：网络/协议异常就地收敛成 `None`，只有真正的
  取消信号继续上抛。
- 新增 `ingest_state()` / `is_ingest_broken()` / `mark_shutdown()`。
- `start_polling()` 返回**主管**任务句柄，cancel 它会连带收尾子任务。

### `src/app.py`

- `/health`：`is_ingest_broken()` 为真时返回 **503 `{"status":"degraded"}`**，
  连续失败触发平台自动重建实例。仅在**确实启动过轮询且不在关停中**时才可能
  为真（webhook 模式、单测进程不受影响），响应体仍不泄露任何内部统计。
- `telegram_worker`：以 `app_state._shutting_down` 为唯一关停判据，误取消
  吸收后继续消费队列；`task_done()` 每次 `get()` 恰好配一次。
- 心跳新增 `ingest=alive:…,age:…s,stalled:…,restarts:…` —— 这是本次排查最缺
  的一列。
- 关停时第一个 `after_serving` 钩子即置 `_shutting_down` 并
  `telegram_polling.mark_shutdown()`。

### 其它

- `src/app_state.py`：新增 `_shutting_down`。
- `src/app_commands.py`：`/webhookinfo` 增加"上次成功拉取 / 通道重启次数"。

## 验收

```
tests/unit/test_ingest_supervisor.py        6 passed   # 三条不变量 + 健康判定
tests/integration/test_app_endpoints.py    15 passed   # 含 /health 200 与 503 两分支
```

（`test_flattened_module_graph_imports` 需要 `mcp` 包，本地验证环境未装，
与本次改动无关。）

## 上线后怎么确认修好了

1. 心跳里应持续出现 `ingest=alive:True,age:<25s,stalled:False,restarts:0`；
2. `age` 长期增长或 `restarts` 上涨 = 通道曾经断过并已自愈，去翻附近的
   CRITICAL 行看原因；
3. 真的救不回来时 `/health` 变 503，Render 自动重建实例，不再需要人肉发现。
