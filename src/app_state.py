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
