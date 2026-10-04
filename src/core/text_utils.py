"""通用小工具：HTML 转义 / 重试装饰器 / 时间文案（自 utils.py 拆出）。"""

import functools
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Awaitable, Callable, TypeVar, cast
from urllib.parse import urlparse

import logging

logger = logging.getLogger(__name__)


# ---------- 工具函数 ----------

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

# 用户可见的 markdown/自然语言文本转义统一走
# markdown_converter.convert_markdown_to_telegram_html()（对纯文本短路
# 原样返回）。escape_html_text 只服务于另一类场景：手工拼装富文本卡片
# 时对**程序产出的动态片段**（标题、计数、文件名等）做严格 & < > 转义，
# 这类内容不是 markdown，走 markdown 转换器反而不会转义裸露的 <、>、&。

def escape_html_text(value: Any) -> str:
    """纯 HTML 转义（& < >，无条件转义 &）：用于卡片 HTML 的动态片段。"""
    s = "" if value is None else str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_domain(url: str) -> str:
    """从 URL 提取展示用域名；空值/无 netloc 时给出兜底文案。

    原先在 ai.error_formatting 与 tool_ui_render 各有一份逐字拷贝，
    现收敛到此（工具结果标题与错误通知都在用它标注来源站点）。
    """
    if not url:
        return "unknown"
    parsed = urlparse(url)
    return parsed.netloc or parsed.path.split('/')[0]
