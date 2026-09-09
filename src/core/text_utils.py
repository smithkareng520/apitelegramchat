"""通用小工具：HTML 转义 / 重试装饰器 / 时间文案（自 utils.py 拆出）。"""

import re
import functools
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Awaitable, Callable, TypeVar, cast

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

# escape_html() 已删除：项目内所有 HTML 转义统一改为调用
# markdown_converter.convert_markdown_to_telegram_html()，不再保留独立
# 的纯转义函数。
#
# 注意（迁移后的行为差异）：convert_markdown_to_telegram_html 对完全不
# 含 markdown 语法的文本会直接原样返回（短路优化），不会转义裸露的
# <、>、&。这与原 escape_html 逐字符转义的行为不同——原调用点里若
# 文本恰好不含任何 markdown 特征（*、`、#、列表符号等）又带有裸露的
# <、>、& ，转换后将不再被转义。
