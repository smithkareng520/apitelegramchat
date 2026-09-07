"""通用小工具：HTML 转义 / 重试装饰器 / 时间文案（自 utils.py 拆出）。"""

import re
import functools
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Awaitable, Callable, Optional, TypeVar, cast

import logging

logger = logging.getLogger(__name__)


# ---------- 工具函数 ----------
_SMART_AMP_PATTERN = re.compile(r'&(?![a-zA-Z0-9#]+;)')

# 可重试异步函数的类型变量：保持装饰后函数的参数/返回类型签名。
_F = TypeVar("_F", bound="Callable[..., Awaitable[Any]]")

def retry_async(max_retries: int = 3, delay: float = 1.0, backoff: float = 3.0, exceptions: tuple[type[BaseException], ...] = (Exception,)) -> Callable[[_F], _F]:
    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            current_delay = delay
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except exceptions as e:
                    # 某些异常（如 MCP 的额度、鉴权和参数错误）已明确标记为不可重试，
                    # 不应为了固定重试次数而额外消耗调用配额或掩盖根因。
                    if attempt == max_retries - 1 or not getattr(e, "retryable", True):
                        raise
                    logger.warning(f"Retry {attempt+1}/{max_retries} for {func.__name__} due to {e}")
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
            return None
        return cast(_F, wrapper)
    return decorator

def get_current_time() -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    return f"{days[now.weekday()]}, {months[now.month - 1]} {now.day}, {now.year}"

def escape_html(text: Optional[str]) -> str:
    """转义 HTML 特殊字符（<、>、&）。

    历史 BUG：此函数曾是一个 no-op（`return text`），导致 60+ 处调用点
    实际上未做任何转义，存在 HTML 注入风险。现做智能 ampersand 处理
    （避免对已有的 &amp;/&#39; 实体二次转义）。
    非字符串输入会被先转换为 str。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    text = _SMART_AMP_PATTERN.sub('&amp;', text)
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    return text
