"""全局 aiohttp ClientSession 懒加载单例（自 utils.py 拆出）。"""

import asyncio
from typing import Optional

import aiohttp

from core.logging_setup import logger


# ---------- HTTP session 复用 ----------
# Rich draft 更新是高频短请求，不能每次 flush 创建新的 ClientSession。
# 使用懒加载单例，复用 TCP/TLS keep-alive 连接。
_http_session: Optional[aiohttp.ClientSession] = None
_http_session_lock = asyncio.Lock()

async def get_http_session() -> aiohttp.ClientSession:
    """获取全局 aiohttp session（用于高频内部 API 请求）。

    session 统一使用默认 30s 总超时；需要更短/更长超时的调用方请在
    具体请求上传入 ``timeout=``（aiohttp 支持按请求覆盖），避免旧实现
    中"首次创建者决定全局超时、后到调用方的超时参数被静默丢弃"的问题。
    """
    global _http_session
    if _http_session is None or _http_session.closed:
        async with _http_session_lock:
            if _http_session is None or _http_session.closed:
                connector = aiohttp.TCPConnector(
                    limit=100,
                    limit_per_host=50,
                    keepalive_timeout=60,
                )
                _http_session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=30),
                )
    return _http_session


async def close_http_session() -> None:
    """应用关闭时显式关闭全局 aiohttp session。

    修复日志中的：
      ERROR asyncio: Unclosed client session
        client_session: <aiohttp.client.ClientSession object at 0x...>
    原因：全局单例只在首次使用时创建，但进程退出前没有任何关闭路径，
    session 只能靠 GC 回收并触发 asyncio 告警。由 app.after_serving
    钩子调用。
    """
    global _http_session
    async with _http_session_lock:
        if _http_session is not None and not _http_session.closed:
            try:
                await _http_session.close()
            except Exception:
                logger.debug("close_http_session 内部忽略的异常", exc_info=True)
                pass
        _http_session = None
