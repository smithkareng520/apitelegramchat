"""app 运行期状态：update 队列与后台任务句柄（自 app.py 拆出）。

集中放置需要被 app.py 生命周期钩子「重新赋值」的全局句柄：
`from app import X` 的 from-import 拿到的是旧绑定，看不到后续重赋值；
改为 `import app_state; app_state.X = ...` 属性访问，读写始终一致。
"""
import asyncio
import os
from typing import Any

WEBHOOK_QUEUE_MAXSIZE = int(os.getenv("WEBHOOK_QUEUE_MAXSIZE", "1000"))

# 有界队列：满时 webhook 入口 429，把背压交还 Telegram（指数退避重投）。
update_queue: "asyncio.Queue[dict[str, Any]]" = asyncio.Queue(maxsize=WEBHOOK_QUEUE_MAXSIZE)

# 后台任务句柄（由 app.py 生命周期钩子赋值/取消）
_telegram_worker_task: asyncio.Task | None = None
_telegram_polling_task: asyncio.Task | None = None
_loop_watchdog_task: asyncio.Task | None = None

# 进程是否正在关停（after_serving 第一步置位）。
#
# 后台常驻循环（telegram_worker / 轮询循环）据此区分两种 CancelledError：
#   · 关停信号 → 正常退出；
#   · 误取消（子任务取消沿 await 链回传、aiohttp 超时取消逸出等）→ 必须
#     吸收并继续，否则一次偶发取消就让 bot 永久失聪：进程活着、/health
#     200、心跳照常，但再也收不到任何消息（2026-09-15 事故现场）。
_shutting_down: bool = False
