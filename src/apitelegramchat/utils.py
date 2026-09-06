# utils.py
import json
import os
import re
import aiohttp
import asyncio
import functools
import html
import logging
from logging import handlers as logging_handlers
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from apitelegramchat.config import BASE_URL, DEEPSEEK_API_KEY, OPENROUTER_API_KEY, LOG_LEVEL
from typing import Optional, Dict, Union
from contextlib import asynccontextmanager
import sys
from apitelegramchat.config import GROQ_API_KEY
from apitelegramchat.markdown_converter import convert_markdown_to_telegram_html

# ---------- 配置日志 ----------
# 日志文件路径可由环境变量 LOG_FILE 覆盖；默认 /tmp/app.log 仅在可写时启用。
LOG_FILE = os.getenv("LOG_FILE", "/tmp/app.log")


class _MCPStreamableHTTPNoiseFilter(logging.Filter):
    """将 MCP SDK 的原始 ERROR traceback 降为一行 WARNING。

    ModelScope 网关偶发截断 JSON 响应体时，SDK 会以 ERROR + 完整
    traceback（40+ 行 httpx/httpcore 堆栈）记录 "Error parsing JSON
    response"。该异常客户端已通过「单次超时 → 分页定向重试 → 部分
    降级」处理，无需整页堆栈刷屏；降级为一行 WARNING 保留根因痕迹
    （字节计数等信息已由 search_engine 的降级日志补足）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # 故意不在这里调用 logger：本方法运行在 logging 过滤器管线内部，
            # 从 filter() 里再发一条日志有重入/递归风险（新日志记录会重新
            # 经过同一套 handler/filter 链）。保持静默放行是安全的选择。
            return True
        if record.levelno >= logging.ERROR and "Error parsing JSON response" in message:
            record.msg = message + "（上游网关截断响应体；已由超时+定向重试+降级处理）"
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True

def setup_logging() -> bool:
    """配置 root logger。返回 True 表示完成了配置，False 表示跳过。

    通过环境变量 APITELEGRAMCHAT_REQUIRE_LOGGING=1 可强制在导入时配置；
    默认情况下，若 root logger 已有 handler 则不再覆盖，便于宿主程序
    （如 unit tests、MCP server）自定义 logging config。
    """
    root_logger = logging.getLogger()
    # 应用 LOG_LEVEL 环境变量（默认 INFO）
    try:
        level = getattr(logging, LOG_LEVEL, logging.INFO)
    except Exception:
        # 注意：本函数在模块级可能于 `logger = logging.getLogger(__name__)`
        # （文件末尾）赋值之前就被调用（见文件底部的 import-time 触发），
        # 此处不能引用模块级 logger，否则会抛 NameError。
        level = logging.INFO
    if root_logger.level == logging.NOTSET or root_logger.level > level:
        root_logger.setLevel(level)
    logging.getLogger('botocore').setLevel(logging.WARNING)
    logging.getLogger('aiobotocore').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    # ModelScope MCP 网关会立即关闭 SSE GET 流，导致 SDK 客户端不停
    # 重连并每次打印一条 INFO（"GET stream disconnected, reconnecting
    # in 1000ms..."），大量冲刷日志。重连本身无害且自动进行，调高该
    # logger 级别降噪；响应体截断的原始 ERROR 则由
    # _MCPStreamableHTTPNoiseFilter 降级为一行 WARNING。
    sdk_logger = logging.getLogger('mcp.client.streamable_http')
    sdk_logger.setLevel(logging.WARNING)
    # 幂等安装：重复调用 setup_logging 不叠加 filter。
    if not any(isinstance(f, _MCPStreamableHTTPNoiseFilter) for f in sdk_logger.filters):
        sdk_logger.addFilter(_MCPStreamableHTTPNoiseFilter())

    # 仅在没有任何 handler 时才安装 console/file handler，
    # 避免重复 import（例如 utils 被 reload）造成 handler 累积和日志重复输出。
    if root_logger.handlers:
        return False

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}'
    ))
    root_logger.addHandler(console_handler)

    try:
        file_handler = logging_handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10*1024*1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}'
        ))
        root_logger.addHandler(file_handler)
    except Exception as e:
        # 同上：此处不能用模块级 logger（可能尚未赋值），保留 print 到 stderr
        # 作为在 logger 就绪前也不会丢失的兜底，同时把详情打全（原来只有 e 的
        # str()，堆栈信息会丢失）。
        import traceback
        print(f"Warning: 无法创建文件日志 {LOG_FILE}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return True

# 仅在显式开启或 root logger 还没有 handler 时执行初始化；无条件覆盖
# root logger 会让 MCP server、tests 等宿主失去对自己 logging 配置的控制。
if os.getenv("APITELEGRAMCHAT_REQUIRE_LOGGING", "0") in {"1", "true", "yes", "on"} or not logging.getLogger().handlers:
    setup_logging()

logger = logging.getLogger(__name__)

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

# ---------- chat 不可达熔断（403 类永久性发送失败） ----------
# 用户把 bot 屏蔽/封禁后，Telegram 对该 chat 的所有发送一律返回
# 403 Forbidden（"bot was blocked by the user" / "user is deactivated" /
# "bot was kicked ..."）；"chat not found"（400）则表示 chat 已不存在。
# 这些都是**永久性**错误：重试、降级富文本、换 sendMessage 都救不回来。
# 白名单管不住这种用户（他仍在白名单里），若不熔断，proactive TIMER 会
# 每 5~20min 触发一轮完整 LLM 回合却永远送达不了，无限空转烧 token。
# 识别到这类错误后统一通知 proactive 停用该 chat 的调度；用户解除屏蔽
# 并再次发消息时由 note_user_activity 自动恢复（详见 proactive.py）。
def _permanent_chat_error_reason(status: int, body: str) -> Optional[str]:
    """判断一次 Telegram 发送失败是否为该 chat 的永久性不可达。

    返回人类可读的原因字符串；非永久性错误（429 限流、5xx、网络错误、
    400 内容错误等）返回 None——那些应该走既有的重试/降级/失败计数路径。
    """
    body_lower = (body or "").lower()
    if status == 403:
        # Telegram Bot API 对 chat 定向的 403 一律是权限级永久失败
        #（bot 被屏蔽 / 账号停用 / 被踢出群）。区别于 401（bot token
        # 级认证失败，会影响所有 chat，不能据此熔断单个 chat）。
        return body[:120] or "403 Forbidden"
    if "chat not found" in body_lower:
        return body[:120] or "chat not found"
    return None


async def _notify_chat_unreachable(chat_id: int, status: int, body: str) -> bool:
    """若该失败是永久性不可达，通知 proactive 熔断该 chat 的主动唤醒。

    返回 True 表示已判定为永久性错误（调用方应立即放弃重试/降级路径）。
    惰性导入 proactive 以保持 utils 作为底层 Telegram 助手模块的分层
    （proactive 不反向依赖 utils，无循环导入风险）。
    """
    try:
        reason = _permanent_chat_error_reason(status, body)
        if reason is None:
            return False
        from apitelegramchat import proactive
        await proactive.notify_chat_unreachable(chat_id, reason=reason)
        return True
    except Exception:
        logger.debug("notify_chat_unreachable 失败（可忽略）", exc_info=True)
        return False


# ---------- 请求ID上下文 ----------
# 使用 contextvars 替代全局 dict，避免并发协程间 request_id 互相覆盖
import contextvars
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="unknown")

def set_request_id(request_id: str):
    _request_id_var.set(request_id)

def get_request_id() -> str:
    return _request_id_var.get()

class RequestIdAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        rid = get_request_id()
        return f"[{rid}] {msg}", kwargs

def get_logger(name):
    return RequestIdAdapter(logging.getLogger(name), {})

# ---------- 工具函数 ----------
_SMART_AMP_PATTERN = re.compile(r'&(?![a-zA-Z0-9#]+;)')

def retry_async(max_retries: int = 3, delay: float = 1.0, backoff: float = 3.0, exceptions: tuple = (Exception,)):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
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
        return wrapper
    return decorator

def get_current_time() -> str:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    months = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
    return f"{days[now.weekday()]}, {months[now.month - 1]} {now.day}, {now.year}"

def escape_html(text) -> str:
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


def _rich_message_html_payload(html_content: str) -> dict:
    """构造符合 InputRichMessage 规范的 HTML 富消息。

    在交付给 Telegram 前，依次跑两道兜底清理：

    1. ``_strip_invalid_media_urls``：剥离 ``src`` 不是合法 http(s) URL
       的 ``<img>``/``<video>``/``<audio>`` 标签。处理 LLM 把附件
       file_name（如 ``photo_AgACAgUA.jpg``）或 file_id 误当成 URL 的情况，
       Telegram 会以 ``RICH_MESSAGE_PHOTO_URL_INVALID`` 拒绝整条消息。

    2. ``_demote_watch_page_videos``：把 ``<video src="WATCH_PAGE_URL">``
       块降级为 ``<a href>`` 链接。处理 LLM 把 YouTube / Bilibili 等
       观看页 URL 误当直链视频嵌入 ``<video src>`` 的情况——这种 URL
       看着合法（``http(s)://`` 开头），但 Telegram 去抓会拿到 HTML
       页面，以 ``RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND`` 拒绝整条消息。
       降级后保留模型生成的 figcaption 文本，让用户仍可点击跳转观看页。
    """
    # 0. Markdown → Telegram HTML 兜底转换。必须排在媒体清理之前：
    #    Markdown 图片 ![alt](url) 此时才会变成 <img src>，从而同样接受
    #    下面两道 URL 合法性检查；若放在之后，伪 URL 图片会绕过校验并
    #    导致整条消息被 Telegram 拒绝。
    #    模型已按提示词输出纯 HTML 时，转换是幂等 no-op。
    normalized = convert_markdown_to_telegram_html(html_content)
    if normalized != html_content:
        logger.info(
            "sendRichMessage 兜底转换：检测到 Markdown 语法，已转为 Telegram HTML。"
            "原始长度=%s，转换后长度=%s",
            len(html_content),
            len(normalized),
        )

    cleaned = _strip_invalid_media_urls(normalized)
    demoted = _demote_watch_page_videos(cleaned)
    if demoted != normalized:
        logger.warning(
            "sendRichMessage 兜底清理：检测到伪 URL 或观看页 URL 媒体块，"
            "已剥离/降级以保证消息送达。原始长度=%s，清理后长度=%s",
            len(normalized),
            len(demoted),
        )
    return {
        "html": demoted,
        "skip_entity_detection": True,
    }


# 匹配完整的 <img ...> 标签（含可选自闭合斜杠），直到第一个 >。
# 不处理 <a href>，因为锚点的非法 href 不会让 Telegram 整条拒绝，
# 且剥离锚点会丢失链接文本。
_MEDIA_SRC_RE = re.compile(
    r'<img\b[^>]*?/?>',
    re.IGNORECASE,
)

# 已知的"观看页"URL 模式——这些 URL 永远不可能是直链视频文件，
# 但模型有时会把它们误嵌入 <video src>。命中后整块降级为 <a> 链接，
# 而不是直接删除，避免丢失模型给的视频标题/figcaption 文本。
#
# 直链视频文件的特征是：URL 末尾通常是 .mp4/.webm/.mov/.m3u8/.ts 等
# 视频扩展名，或者来自已知视频 CDN（googlevideo.com、bilivideo.com、
# akamaized.net 等）。这里走"否定式"判定——只要 URL 命中观看页模式，
# 就一定不是直链。
#
# 顺序无所谓，命中任一即认定为观看页。
_WATCH_PAGE_URL_PATTERNS = (
    # YouTube watch / embed / shorts —— 注意 watch 后面通常跟 ? 而不是 /
    re.compile(r'youtube\.com/watch\b', re.IGNORECASE),
    re.compile(r'youtube\.com/embed/', re.IGNORECASE),
    re.compile(r'youtube\.com/shorts/', re.IGNORECASE),
    re.compile(r'youtube\.com/live/', re.IGNORECASE),
    re.compile(r'youtu\.be/', re.IGNORECASE),
    # Bilibili 观看页
    re.compile(r'bilibili\.com/video/', re.IGNORECASE),
    re.compile(r'bilibili\.com/bangumi/play/', re.IGNORECASE),
    re.compile(r'b23\.tv/', re.IGNORECASE),
    # Vimeo / Dailymotion / Twitch / TikTok / Facebook / X / Nico
    re.compile(r'vimeo\.com/\d', re.IGNORECASE),
    re.compile(r'dailymotion\.com/video/', re.IGNORECASE),
    re.compile(r'twitch\.tv/videos/', re.IGNORECASE),
    re.compile(r'tiktok\.com/@', re.IGNORECASE),
    re.compile(r'facebook\.com/(?:watch|reel)/', re.IGNORECASE),
    re.compile(r'fb\.watch/', re.IGNORECASE),
    re.compile(r'(?:twitter|x)\.com/[^/]+/status/', re.IGNORECASE),
    re.compile(r'nico(?:video)?\.[a-z]+/watch/', re.IGNORECASE),
)

def _looks_like_watch_page(url: str) -> bool:
    """判定 URL 是否为视频观看页（不可作为 <video src> 直链）。"""
    if not url:
        return False
    u = url.strip()
    if not (u.lower().startswith("http://") or u.lower().startswith("https://")):
        return False
    # 命中观看页模式即认定非直链
    for pat in _WATCH_PAGE_URL_PATTERNS:
        if pat.search(u):
            return True
    return False


def _strip_invalid_media_urls(html_content: str) -> str:
    """剥离 src 不是合法 http(s) URL 的 <img>/<video>/<audio> 标签。

    Telegram Rich Message 要求 ``<img src>`` 必须是公开可访问的 http(s)
    URL；LLM 偶尔会把附件 file_name（如 ``photo_AgACAgUA.jpg``）或 file_id
    当作 URL 写入，会被服务端以 ``RICH_MESSAGE_PHOTO_URL_INVALID`` 拒绝。

    本函数逐个扫描 ``<img>``/``<video>``/``<audio>`` 标签的 ``src`` 属性，
    若不以 ``http://`` 或 ``https://`` 开头，则：

      * 对 ``<img .../>`` 自闭合形式：直接删除整个标签；
      * 对 ``<video>...</video>`` / ``<audio>...</audio>`` 容器形式：
        删除整个开闭标签对（包含内部内容），避免出现孤立结束标签；
      * 若外层是 ``<figure>`` 且剥离后 figure 内既无 ``<img>``/``<video>``
        也无 ``<figcaption>``，则一并删除该空 figure。

    另外，对 ``<video src="WATCH_PAGE_URL">`` 还会做"观看页降级"：
    若 URL 命中 YouTube / Bilibili / Vimeo / 抖音 / Twitter / Facebook
    等观看页模式（即非直链视频文件），把整个 ``<figure>`` 块降级为
    ``<a href="URL">CAPTION</a>``，避免 Telegram 以
    ``RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND`` 拒绝整条消息——同时保留模型
    生成的视频标题/figcaption 文本，让用户仍可点击跳转观看页。
    """
    if not html_content:
        return ""
    def _extract_src(tag_text: str) -> str:
        m = re.search(r'\bsrc\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
                      tag_text, re.IGNORECASE)
        if not m:
            return ""
        return (m.group(2) or m.group(3) or m.group(4) or "").strip()

    def _is_valid_url(url: str) -> bool:
        u = (url or "").strip().lower()
        return bool(u) and u.startswith(("http://", "https://"))

    # 先处理容器型 <video>...</video> / <audio>...</audio>：
    # 起始标签 src 非法时，连同内部内容一起删掉。
    def _strip_container(text: str, tag: str) -> str:
        pattern = re.compile(
            rf'<{tag}\b[^>]*>.*?</{tag}\s*>|<{tag}\b[^>]*/>',
            re.IGNORECASE | re.DOTALL,
        )

        def _check(m: re.Match) -> str:
            block = m.group(0)
            src = _extract_src(block)
            if _is_valid_url(src):
                return block  # 合法 URL，保留
            return ""  # 非法 URL，整块删除

        return pattern.sub(_check, text)

    result = html_content
    for tag in ("video", "audio"):
        result = _strip_container(result, tag)

    # 再处理 <img .../> 自闭合形式
    def _strip_img(text: str) -> str:
        def _check(m: re.Match) -> str:
            block = m.group(0)
            src = _extract_src(block)
            if _is_valid_url(src):
                return block
            return ""
        # <img ...> 不一定有自闭合斜杠，统一处理
        return _MEDIA_SRC_RE.sub(_check, text)

    result = _strip_img(result)

    # 清理空的 <figure>...</figure>（剥离后只剩空白）
    figure_pattern = re.compile(
        r'<figure\b[^>]*>(.*?)</figure\s*>',
        re.IGNORECASE | re.DOTALL,
    )

    def _clean_empty_figure(m: re.Match) -> str:
        inner = m.group(1) or ""
        # 仍然有 img/video/audio/figcaption 就保留
        has_media = re.search(
            r'<(img|video|audio|figcaption)\b',
            inner,
            re.IGNORECASE,
        )
        if has_media:
            return m.group(0)
        # 全空白：直接删
        if not inner.strip():
            return ""
        return m.group(0)

    result = figure_pattern.sub(_clean_empty_figure, result)
    return result


def _demote_watch_page_videos(html_content: str) -> str:
    """把 ``<video src="WATCH_PAGE_URL">`` 块降级为 ``<a href>`` 链接。

    在 ``_strip_invalid_media_urls`` 之后跑——前者只检查 URL 是否
    以 ``http(s)://`` 开头，无法识别"看着合法但其实是 HTML 观看页"的
    URL（如 ``https://www.bilibili.com/video/BVxxx``）。Telegram 收到
    这种 ``<video>`` 后会去抓 URL 当视频文件，拿到 HTML 页面，以
    ``RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND`` 拒绝整条消息。

    本函数扫描所有 ``<video src="...">`` 标签，若 URL 命中观看页模式，
    把整个 ``<figure><video src="URL"></video><figcaption>CAPTION</figcaption></figure>``
    降级为 ``<a href="URL">🎬 CAPTION</a>``。若没有外层 ``<figure>``，
    则把 ``<video>...</video>`` 单独替换为 ``<a>`` 链接。

    URL 看起来是直链（命中 .mp4/.webm/.mov/.m3u8/.ts/.ogg 等）时，
    保留原 ``<video>`` 不动——这种情况 Telegram 通常能正常播放。
    """
    if not html_content:
        return ""

    # 先匹配 <figure><video ...>...</video><figcaption>...</figcaption></figure>
    # 也兼容 <figure><video .../></figure>（自闭合）与 figcaption 在 video 之前。
    figure_video_re = re.compile(
        r'<figure\b[^>]*>(.*?)</figure\s*>',
        re.IGNORECASE | re.DOTALL,
    )
    video_in_figure_re = re.compile(
        r'<video\b[^>]*>.*?</video\s*>|<video\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_figure(m: re.Match) -> str:
        inner = m.group(1) or ""
        # 找到第一个 <video> 块
        vm = video_in_figure_re.search(inner)
        if not vm:
            return m.group(0)  # 没有 video，原样返回

        video_block = vm.group(0)
        src = _extract_attr(video_block, "src")
        if not src:
            return m.group(0)  # 无 src，保留给 _strip_invalid_media_urls 处理

        # 只有命中观看页模式才降级；否则保留原 <video>，
        # 让 Telegram 自行处理（直链成功就播，失败由后续兜底）。
        if not _looks_like_watch_page(src):
            return m.group(0)

        # 命中观看页模式 → 降级为 <a> 链接。
        # caption 优先取 <figcaption> 文本，退化到 domain。
        figcaption_re = re.compile(
            r'<figcaption\b[^>]*>(.*?)</figcaption\s*>',
            re.IGNORECASE | re.DOTALL,
        )
        figcaption_text = ""
        figm = figcaption_re.search(inner)
        if figm:
            figcaption_text = re.sub(r'<[^>]+>', '', figm.group(1)).strip()

        if not figcaption_text:
            domain = _domain_of(src)
            figcaption_text = "🎬 观看视频" + (f" · {domain}" if domain else "")

        # 把 video 块和 figcaption 都从 inner 里去掉，剩余内容（少见）追加在链接后
        rest = video_in_figure_re.sub("", inner)
        rest = figcaption_re.sub("", rest).strip()

        anchor = f'<a href="{src}"><b>{figcaption_text}</b></a>'
        if not rest:
            return anchor
        return f"{anchor} {rest}"

    def _extract_attr(tag_text: str, attr: str) -> str:
        m = re.search(
            rf'\b{attr}\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
            tag_text, re.IGNORECASE,
        )
        if not m:
            return ""
        return (m.group(2) or m.group(3) or m.group(4) or "").strip()

    def _extract_caption_from_inner(inner: str) -> str:
        m = re.search(
            r'<figcaption\b[^>]*>(.*?)</figcaption\s*>',
            inner, re.IGNORECASE | re.DOTALL,
        )
        if m:
            text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
            if text:
                return text
        bare = re.sub(r'<[^>]+>', ' ', inner)
        bare = re.sub(r'\s+', ' ', bare).strip()
        return bare[:80] if bare else ""

    def _domain_of(url: str) -> str:
        m = re.match(r'https?://([^/\s]+)', url or "")
        return m.group(1) if m else ""

    result = figure_video_re.sub(_replace_figure, html_content)

    # 再处理"裸" video（不在 <figure> 里的，或 figure 已经被上面处理过
    # 但 video 漏在 figure 外的零散情况）。
    bare_video_re = re.compile(
        r'<video\b[^>]*>.*?</video\s*>|<video\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_bare_video(m: re.Match) -> str:
        block = m.group(0)
        src = _extract_attr(block, "src")
        if not src:
            return block  # 留给 _strip_invalid_media_urls 删除
        if not _looks_like_watch_page(src):
            return block  # 不是观看页，保留
        # 退化 caption：取 video 块内文本，再不行就用 domain
        caption = _extract_caption_from_inner(block)
        if not caption:
            domain = _domain_of(src)
            caption = "🎬 观看视频" + (f" · {domain}" if domain else "")
        return f'<a href="{src}"><b>{caption}</b></a>'

    result = bare_video_re.sub(_replace_bare_video, result)
    return result


def _extract_media_urls(html_content: str, media_kind: str) -> list[str]:
    """提取指定类型媒体的所有 src URL。
    
    Args:
        html_content: HTML 内容
        media_kind: 媒体类型，可以是 "img", "video", "audio"
    
    Returns:
        该类型所有媒体的 src URL 列表（按出现顺序）
    """
    if not html_content or not media_kind:
        return []
    
    urls = []
    # 匹配该类型的所有标签（包括自闭合和容器形式）
    pattern = re.compile(
        rf'<{media_kind}\b[^>]*>.*?</{media_kind}\s*>|<{media_kind}\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )
    
    for match in pattern.finditer(html_content):
        tag = match.group(0)
        # 提取 src 属性
        src_match = re.search(r'\bsrc\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag, re.IGNORECASE)
        if src_match:
            src = (src_match.group(2) or src_match.group(3) or src_match.group(4) or "").strip()
            if src:
                urls.append(src)
    
    return urls


def _demote_specific_media_url(html_content: str, media_kind: str, target_url: str) -> str:
    """只降级指定 URL 的媒体，保留其他媒体不变。
    
    Args:
        html_content: HTML 内容
        media_kind: 媒体类型 ("img", "video", "audio")
        target_url: 要降级的目标 URL
    
    Returns:
        处理后的 HTML（只有目标 URL 的媒体被降级为链接）
    """
    if not html_content or not target_url:
        return html_content
    
    def _extract_src(tag_text: str) -> str:
        m = re.search(r'\bsrc\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag_text, re.IGNORECASE)
        if not m:
            return ""
        return (m.group(2) or m.group(3) or m.group(4) or "").strip()
    
    def _domain_of(url: str) -> str:
        m = re.match(r'https?://([^/\s]+)', url or "")
        return m.group(1) if m else ""
    
    def _figcaption_text(inner: str) -> str:
        m = re.search(r'<figcaption\b[^>]*>(.*?)</figcaption\s*>', inner, re.IGNORECASE | re.DOTALL)
        if not m:
            return ""
        return re.sub(r'<[^>]+>', '', m.group(1)).strip()
    
    def _build_anchor(src: str, caption: str, kind: str) -> str:
        if not caption:
            domain = _domain_of(src)
            label_map = {"video": "🎬 观看视频", "audio": "🎵 收听音频", "img": "🖼 查看图片"}
            caption = label_map.get(kind, "🔗 查看链接")
            if domain:
                caption = f"{caption} · {domain}"
        safe_caption = caption.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<a href="{src}"><b>{safe_caption}</b></a>'
    
    # 1) 处理 <figure> 内的目标媒体
    figure_re = re.compile(r'<figure\b[^>]*>(.*?)</figure\s*>', re.IGNORECASE | re.DOTALL)
    media_in_figure_re = re.compile(
        rf'<{media_kind}\b[^>]*?/?>.*?</{media_kind}\s*>|<{media_kind}\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )
    
    def _replace_figure(m: re.Match) -> str:
        inner = m.group(1) or ""
        mm = media_in_figure_re.search(inner)
        if not mm:
            return m.group(0)
        
        block = mm.group(0)
        src = _extract_src(block)
        
        # 只处理目标 URL
        if src != target_url:
            return m.group(0)
        
        cap_text = _figcaption_text(inner)
        anchor = _build_anchor(src, cap_text, media_kind)
        
        # 移除媒体块和 figcaption，保留其他内容
        rest = inner.replace(block, "")
        rest = re.sub(r'<figcaption\b[^>]*>.*?</figcaption\s*>', '', rest, flags=re.IGNORECASE | re.DOTALL)
        rest = rest.strip()
        
        if rest:
            return f"{anchor} {rest}"
        return anchor
    
    result = figure_re.sub(_replace_figure, html_content)
    
    # 2) 处理裸媒体（不在 <figure> 中的）
    bare_media_re = re.compile(
        rf'<{media_kind}\b[^>]*>.*?</{media_kind}\s*>|<{media_kind}\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )
    
    def _replace_bare(m: re.Match) -> str:
        block = m.group(0)
        src = _extract_src(block)
        
        # 只处理目标 URL
        if src != target_url:
            return block
        
        caption = _figcaption_text(block)
        if not caption:
            bare = re.sub(r'<[^>]+>', ' ', block)
            caption = re.sub(r'\s+', ' ', bare).strip()[:80]
        
        return _build_anchor(src, caption, media_kind)
    
    result = bare_media_re.sub(_replace_bare, result)
    
    return result


async def _selective_media_fallback(
    session: aiohttp.ClientSession,
    base_url: str,
    original_payload: dict,
    html_content: str,
    media_kinds: set[str],
) -> Optional[int]:
    """逐个排查有问题的媒体，只降级有问题的那个，保留其他正常媒体。
    
    策略：
    1. 对每个报错的媒体类型，提取所有该类型的媒体 URL
    2. 逐个尝试只降级其中一个 URL，测试消息能否发送成功
    3. 找到有问题的 URL 后，返回成功的消息 ID
    4. 如果逐个都试完了还是失败，返回 None（让调用方执行全部降级兜底）
    
    Args:
        session: aiohttp 会话
        base_url: Telegram API base URL
        original_payload: 原始请求 payload
        html_content: 原始 HTML 内容
        media_kinds: 报错的媒体类型集合 (如 {"img", "video"})
    
    Returns:
        成功时返回 message_id (int)，失败返回 None
    """
    logger.info(
        "开始逐个排查有问题的媒体，类型: %s",
        sorted(media_kinds),
    )
    
    # 对每个媒体类型，提取所有 URL
    for kind in sorted(media_kinds):
        urls = _extract_media_urls(html_content, kind)
        if not urls:
            continue
        
        logger.debug("媒体类型 %s 共有 %d 个实例: %s", kind, len(urls), urls[:3])
        
        # 逐个尝试只降级其中一个
        for idx, url in enumerate(urls):
            test_html = _demote_specific_media_url(html_content, kind, url)
            if test_html == html_content:
                continue  # 没有变化，跳过
            
            test_payload = {
                **original_payload,
                "rich_message": _rich_message_html_payload(test_html),
            }
            
            logger.debug(
                "测试降级第 %d/%d 个 %s: %s",
                idx + 1, len(urls), kind, url[:80],
            )
            
            try:
                async with session.post(f"{base_url}/sendRichMessage", json=test_payload) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            msg_id = (data.get("result") or {}).get("message_id")
                            if isinstance(msg_id, int) and msg_id > 0:
                                logger.info(
                                    "成功定位有问题的媒体并只降级它: kind=%s url=%s",
                                    kind, url[:100],
                                )
                                return msg_id
                        except Exception as e:
                            logger.debug("解析响应失败: %s", e)
                        return True  # 成功但无法解析 message_id
            except Exception as e:
                logger.debug("测试请求异常: %s", e)
                continue
    
    logger.info("逐个排查完成，未找到单一问题媒体，将执行全部降级兜底")
    return None


def _demote_all_media_to_links(
    html_content: str,
    media_kinds: Optional[set[str]] = None,
) -> str:
    """按指定媒体类型把 ``<video>/<audio>/<img>`` 降级为 ``<a href>``。

    ``media_kinds`` 为 ``None`` 时保持兼容；传入 ``{"image"}``、
    ``{"video"}`` 或 ``{"audio"}`` 时只处理对应类型，避免某一种媒体
    失败时牵连同一条消息中的其他媒体。

    用于 ``sendRichMessage`` 因媒体 URL 在 Telegram 服务端抓取失败而拒绝
    整条消息时的**反应式兜底**——例如：

      * LLM 嵌入了 ``<video src="...upload.wikimedia.org/.../x.ogv.480p.vp9.webm">``，
        URL 在浏览器侧能拿到 200 + ``video/webm``，但 Telegram 媒体抓取
        服务对该格式 / 编解码 / host 的支持有限，返回
        ``RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND``，导致**整条消息丢失**；
      * LLM 嵌入了看似合法的 ``<img src>`` 但目标服务端拒绝 Telegram
        bot 的 User-Agent，返回 ``RICH_MESSAGE_PHOTO_URL_INVALID`` 等。

    与 ``_demote_watch_page_videos``（仅在 URL 命中已知观看页模式时降级）
    不同，本函数**无差别降级所有媒体**——只在初次发送已经被服务端拒绝
    之后才触发，因此激进策略是安全的：宁可丢失"内嵌播放器"也不可丢失
    整条用户可见的回复。

    降级规则：

      * ``<figure><video src="URL"></video><figcaption>CAP</figcaption></figure>``
        → ``<a href="URL"><b>🎬 CAP</b></a>``（保留 figcaption 文本，让用户
        仍能点击跳转到原媒体页）；
      * 裸 ``<video src="URL"></video>`` 或自闭合 ``<video src="URL"/>``
        → ``<a href="URL"><b>🎬 观看视频 · {domain}</b></a>``；
      * ``<figure><img src="URL"/><figcaption>CAP</figcaption></figure>``
        → ``<a href="URL"><b>🖼 CAP</b></a>``；
      * 裸 ``<img src="URL"/>`` → ``<a href="URL"><b>🖼 {domain}</b></a>``；
      * ``<audio>`` 同理用 🎵 前缀；
      * ``src`` 非法（非 http(s):// 开头）的媒体块直接整块删除——这种
        URL 在 ``_strip_invalid_media_urls`` 阶段也已被处理，这里是冗余
        兜底。
    """
    if not html_content:
        return ""
    if media_kinds is not None:
        media_kinds = {str(kind).lower() for kind in media_kinds}

    def _extract_src(tag_text: str) -> str:
        m = re.search(r'\bsrc\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
                      tag_text, re.IGNORECASE)
        if not m:
            return ""
        return (m.group(2) or m.group(3) or m.group(4) or "").strip()

    def _is_valid_url(url: str) -> bool:
        u = (url or "").strip().lower()
        return bool(u) and u.startswith(("http://", "https://"))

    def _domain_of(url: str) -> str:
        m = re.match(r'https?://([^/\s]+)', url or "")
        return m.group(1) if m else ""

    def _strip_inner_tags(inner: str) -> str:
        """去掉 inner 里所有 <video>/<audio>/<img>/<figcaption> 标签，
        仅保留可能存在的其他内联文本。"""
        inner = re.sub(
            r'<(?:video|audio|img)\b[^>]*/?>|</?(?:video|audio|img)\s*>',
            '', inner, flags=re.IGNORECASE,
        )
        # figcaption 文本由调用方单独提取，这里把已取过文本的空标签也清掉
        inner = re.sub(
            r'<figcaption\b[^>]*>.*?</figcaption\s*>|</?figcaption\s*>',
            '', inner, flags=re.IGNORECASE | re.DOTALL,
        )
        return inner

    def _figcaption_text(inner: str) -> str:
        m = re.search(
            r'<figcaption\b[^>]*>(.*?)</figcaption\s*>',
            inner, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return ""
        text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        return text

    def _build_anchor(src: str, caption: str, kind: str) -> str:
        """构造降级后的 <a> 锚点。src 必须合法；caption 为空时按 kind 兜底。"""
        if not caption:
            domain = _domain_of(src)
            label_map = {"video": "🎬 观看视频", "audio": "🎵 收听音频", "image": "🖼 查看图片"}
            caption = label_map.get(kind, "🔗 查看链接")
            if domain:
                caption = f"{caption} · {domain}"
        # 转义 caption 中的 < > & 防止破坏 HTML 结构
        safe_caption = caption.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<a href="{src}"><b>{safe_caption}</b></a>'

    # 1) 处理 <figure>...</figure> 内的媒体（含 figcaption）
    figure_re = re.compile(
        r'<figure\b[^>]*>(.*?)</figure\s*>',
        re.IGNORECASE | re.DOTALL,
    )
    media_in_figure_re = re.compile(
        r'<(video|audio|img)\b[^>]*?/?>.*?</\1\s*>|<(video|audio|img)\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_figure(m: re.Match) -> str:
        inner = m.group(1) or ""
        mm = media_in_figure_re.search(inner)
        if not mm:
            # 没有 media——保留原 figure（可能是纯文字 figure）
            return m.group(0)
        # 取 media 标签名（video / audio / img）
        kind = (mm.group(1) or mm.group(2) or "").lower()
        block = mm.group(0)
        src = _extract_src(block)
        # figcaption 文本先提取，不论 src 是否合法，都应作为可见内容保留
        cap_text = _figcaption_text(inner)
        if media_kinds is not None and kind not in media_kinds:
            return m.group(0)
        if not src or not _is_valid_url(src):
            # 非法 src：删除该 media 块，但保留 figcaption 文本作为可见内容
            rest = inner.replace(block, "")
            rest = _strip_inner_tags(rest).strip()
            if cap_text:
                rest = f"{cap_text} {rest}".strip() if rest else cap_text
            return rest or ""
        anchor = _build_anchor(src, cap_text, kind)
        # 把 media 块和 figcaption 都从 inner 里去掉，剩余文本追加在后
        rest = _strip_inner_tags(inner.replace(block, "")).strip()
        if rest:
            return f"{anchor} {rest}"
        return anchor

    result = figure_re.sub(_replace_figure, html_content)

    # 2) 处理裸 media（不在 <figure> 里的）
    bare_media_re = re.compile(
        r'<(video|audio)\b[^>]*>.*?</\1\s*>|<(video|audio|img)\b[^>]*/>',
        re.IGNORECASE | re.DOTALL,
    )

    def _replace_bare_media(m: re.Match) -> str:
        block = m.group(0)
        kind = (m.group(1) or m.group(2) or "").lower()
        src = _extract_src(block)
        if media_kinds is not None and kind not in media_kinds:
            return block
        if not src or not _is_valid_url(src):
            return ""  # 非法 src：整块删除
        # 从 block 内部提取可能的 figcaption / 裸文本作为 caption
        caption = _figcaption_text(block)
        if not caption:
            bare = re.sub(r'<[^>]+>', ' ', block)
            bare = re.sub(r'\s+', ' ', bare).strip()
            caption = bare[:80] if bare else ""
        return _build_anchor(src, caption, kind)

    result = bare_media_re.sub(_replace_bare_media, result)

    # 3) 清理可能残留的空 <figure>...</figure>（内部 media 已被替换为 <a>，
    #    figure 容器不再需要）
    result = re.sub(
        r'<figure\b[^>]*>\s*</figure\s*>',
        '', result, flags=re.IGNORECASE,
    )
    return result


def strip_html_tags(text: str) -> str:
    if not text:
        return ""
    text = text.replace("<br/>", "\n").replace("<br>", "\n")
    return re.sub(r'<[^>]*>', '', text)


def _rich_message_plain_text_fallback(html_content: str) -> str:
    """将被 Rich Message 服务端拒绝的 HTML 降级为一个安全的段落。

    模型或工具输出有时仅包含内联标签，或在 ``details`` 中缺少块级内容。此类
    HTML 视觉上并非空白，但 Telegram 会返回 ``RICH_MESSAGE_CONTENT_REQUIRED``。
    这里保留其可见文本并转义为单个 ``<p>``，以保证用户不会因格式问题丢失回复。

    顺序很重要：先 strip 标签，再 unescape 实体，最后**重新转义**输出，避免
    上游 HTML 中的 ``&lt;script&gt;`` 被 unescape 后再次注入回最终 HTML。
    """
    visible_text = strip_html_tags(html_content or "")
    visible_text = html.unescape(visible_text)
    visible_text = re.sub(r"\s+", " ", visible_text).strip()
    if not visible_text:
        return ""
    # 重新转义，确保 unescape 出来的 <、>、& 不会被当作 HTML。
    visible_text = _SMART_AMP_PATTERN.sub('&amp;', visible_text)
    visible_text = visible_text.replace('<', '&lt;').replace('>', '&gt;')
    return f"<p>{visible_text}</p>"

class BalanceResult(dict):
    """统一的余额查询结果，兼容字典访问。"""


async def _fetch_json(session: aiohttp.ClientSession, url: str, api_key: str) -> tuple[int, dict | None, str | None]:
    """发送一次余额请求并返回 HTTP 状态码、JSON 和错误信息。"""
    if not api_key:
        return 0, None, "未配置 API Key"
    try:
        async with session.get(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
        ) as response:
            try:
                data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                data = None
            if response.status != 200:
                return response.status, data, f"HTTP {response.status}"
            return response.status, data, None
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.debug("余额查询请求失败: %s", url, exc_info=True)
        return 0, None, str(exc)[:100]


async def _query_deepseek_balance(session: aiohttp.ClientSession) -> BalanceResult:
    status, data, error = await _fetch_json(
        session,
        "https://api.deepseek.com/user/balance",
        DEEPSEEK_API_KEY,
    )
    if error:
        return BalanceResult(provider="DeepSeek", ok=False, error=error)
    try:
        info = data["balance_infos"][0]
        return BalanceResult(
            provider="DeepSeek",
            ok=True,
            available=data.get("is_available"),
            remaining=info["total_balance"],
            currency=info["currency"],
            granted_balance=info.get("granted_balance"),
            topped_up_balance=info.get("topped_up_balance"),
        )
    except (KeyError, IndexError, TypeError):
        return BalanceResult(provider="DeepSeek", ok=False, error="响应格式异常")


async def _query_openrouter_balance(session: aiohttp.ClientSession) -> BalanceResult:
    status, data, error = await _fetch_json(
        session,
        "https://openrouter.ai/api/v1/key",
        OPENROUTER_API_KEY,
    )
    if error:
        return BalanceResult(provider="OpenRouter", ok=False, error=error)
    try:
        info = data["data"]
        remaining = info.get("limit_remaining")
        return BalanceResult(
            provider="OpenRouter",
            ok=True,
            available=True,
            remaining=remaining,
            currency="USD",
            limit=info.get("limit"),
            usage=info.get("usage"),
            usage_daily=info.get("usage_daily"),
            usage_monthly=info.get("usage_monthly"),
            unlimited=remaining is None,
        )
    except (KeyError, TypeError):
        return BalanceResult(provider="OpenRouter", ok=False, error="响应格式异常")


_BALANCE_QUERYERS = {
    "deepseek": _query_deepseek_balance,
    "ds": _query_deepseek_balance,
    "openrouter": _query_openrouter_balance,
    "or": _query_openrouter_balance,
}


async def query_provider_balances(provider: str | None = None) -> list[BalanceResult]:
    """并发查询已适配厂商的余额；不传 provider 时查询全部已适配厂商。"""
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if provider:
            queryer = _BALANCE_QUERYERS.get(provider.lower())
            if queryer is None:
                return [BalanceResult(provider=provider, ok=False, error="暂未适配公开余额接口")]
            return [await queryer(session)]
        return list(await asyncio.gather(
            _query_deepseek_balance(session),
            _query_openrouter_balance(session),
        ))



class RateLimitError(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")

@retry_async(max_retries=5, delay=0.5, backoff=3.0, exceptions=(aiohttp.ClientError, asyncio.TimeoutError, RateLimitError))
async def delete_message(chat_id: int, message_id: int) -> None:
    from apitelegramchat.state import deleted_message_ids, deleted_messages_lock, is_protected_message
    if await is_protected_message(message_id):
        logger.info(f"deleteMessage 跳过受保护消息: chat={chat_id} msg={message_id}")
        return
    async with deleted_messages_lock:
        if message_id in deleted_message_ids:
            return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{BASE_URL}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            ) as r:
                if r.status == 200:
                    async with deleted_messages_lock:
                        deleted_message_ids.add(message_id)
                    logger.debug(f"deleteMessage 成功: chat={chat_id} msg={message_id}")
                    return
                elif r.status == 429:
                    retry_after = int(r.headers.get("Retry-After", 5))
                    raise RateLimitError(retry_after)
                elif r.status == 400:
                    # "message to delete not found"：消息已不存在（例如草稿
                    # 气泡已被永久消息挤掉）。视为幂等成功，否则会被重试
                    # 装饰器白白发 5 次请求、耗时 6.5s 后仍抛异常。
                    body = await r.text()
                    if "not found" in body.lower():
                        async with deleted_messages_lock:
                            deleted_message_ids.add(message_id)
                        logger.debug(
                            f"deleteMessage 幂等成功（消息已不存在）: chat={chat_id} msg={message_id}"
                        )
                        return
                    logger.error(f"deleteMessage 失败 HTTP 400: {body[:200]}")
                    raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message=body)
                else:
                    body = await r.text()
                    logger.error(f"deleteMessage 失败 HTTP {r.status}: {body[:200]}")
                    raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message=body)
    except Exception as e:
        logger.exception(f"deleteMessage 异常: chat={chat_id} msg={message_id} {e}")
        raise

async def delete_message_fast(chat_id: int, message_id: int) -> bool:
    if not message_id:
        return False
    try:
        from apitelegramchat.state import deleted_message_ids, deleted_messages_lock
        async with deleted_messages_lock:
            if message_id in deleted_message_ids:
                return True
    except Exception:
        logger.debug("delete_message_fast 内部忽略的异常", exc_info=True)
        pass

    timeout = aiohttp.ClientTimeout(total=3, connect=2)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                f"{BASE_URL}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            ) as r:
                if r.status == 200:
                    try:
                        from apitelegramchat.state import deleted_message_ids, deleted_messages_lock
                        async with deleted_messages_lock:
                            deleted_message_ids.add(message_id)
                    except Exception:
                        logger.debug("delete_message_fast 内部忽略的异常", exc_info=True)
                        pass
                    return True
                if r.status == 400:
                    body = await r.text()
                    if "not found" in body.lower() or "to delete not found" in body.lower():
                        try:
                            from apitelegramchat.state import deleted_message_ids, deleted_messages_lock
                            async with deleted_messages_lock:
                                deleted_message_ids.add(message_id)
                        except Exception:
                            logger.debug("delete_message_fast 内部忽略的异常", exc_info=True)
                            pass
                        return True
                return False
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.debug(f"delete_message_fast 失败: chat={chat_id} msg={message_id} {e}")
        return False

# ==================== Rich Message 支持 ====================
_last_sent_draft_cache = {}
_draft_send_locks: dict = {}
_draft_failure_counts: dict = {}
_dead_draft_ids: set = set()
_dead_draft_ids_lock = asyncio.Lock()
_draft_locks_lock = asyncio.Lock()
_draft_last_send_time: dict = {}
_DRAFT_MIN_INTERVAL = 0.25
# 草稿是可被后续完整状态替代的瞬态 UI；不能像永久消息一样在发送锁中
# 连续执行长超时重试，否则一帧网络抖动会让所有后续 Agent 状态长时间排队。
_DRAFT_REQUEST_TIMEOUT = 5.0
_DRAFT_CONNECT_TIMEOUT = 2.5
_DRAFT_MAX_ATTEMPTS = 2
_DRAFT_RETRY_DELAY = 0.25

async def _get_draft_send_lock(chat_id: int, draft_id: int) -> asyncio.Lock:
    key = (chat_id, draft_id)
    async with _draft_locks_lock:
        lock = _draft_send_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _draft_send_locks[key] = lock
        return lock

async def _reset_draft_failure(chat_id: int, draft_id: int) -> None:
    async with _draft_locks_lock:
        _draft_failure_counts.pop((chat_id, draft_id), None)

async def _bump_draft_failure(chat_id: int, draft_id: int) -> int:
    key = (chat_id, draft_id)
    async with _draft_locks_lock:
        _draft_failure_counts[key] = _draft_failure_counts.get(key, 0) + 1
        return _draft_failure_counts[key]

async def _cleanup_dead_draft_state(chat_id: int, draft_id: int) -> None:
    """草稿生命周期结束后，主动清理所有相关缓存项。

    此前 _last_sent_draft_cache / _draft_send_locks / _draft_failure_counts /
    _draft_last_send_time 这 4 个 module-level dict 没有清理路径，长时间运行
    会让每个草稿的元数据永久驻留，造成内存泄漏。这里在 mark_draft_dead 之后
    统一回收。
    """
    if not isinstance(chat_id, int) or not isinstance(draft_id, int):
        return
    key = (chat_id, draft_id)
    async with _draft_locks_lock:
        _last_sent_draft_cache.pop(key, None)
        _draft_send_locks.pop(key, None)
        _draft_failure_counts.pop(key, None)
        _draft_last_send_time.pop(key, None)

async def mark_draft_dead(draft_id) -> None:
    try:
        draft_id_int = int(draft_id)
    except (ValueError, TypeError):
        return
    async with _dead_draft_ids_lock:
        _dead_draft_ids.add(draft_id_int)
    logger.info(f"Draft {draft_id_int} marked as dead")
    # 顺手清理可能仍持有的草稿状态。chat_id 在 mark 阶段无法可靠得到，
    # 我们只能扫描所有 (chat_id, draft_id_int) 键，但数量通常很小。
    async with _draft_locks_lock:
        stale_keys = [k for k in _last_sent_draft_cache if isinstance(k, tuple) and len(k) == 2 and k[1] == draft_id_int]
    for key in stale_keys:
        await _cleanup_dead_draft_state(key[0], draft_id_int)

async def is_draft_dead(draft_id) -> bool:
    try:
        draft_id_int = int(draft_id)
    except (ValueError, TypeError):
        return True
    async with _dead_draft_ids_lock:
        return draft_id_int in _dead_draft_ids

async def _is_current_active_draft(chat_id: int, draft_id) -> bool:
    """只有当前仍然是活跃草稿时才允许继续刷新。"""
    try:
        draft_id_int = int(draft_id)
    except (ValueError, TypeError):
        return False
    try:
        from apitelegramchat.state import get_active_draft_info
        info = await get_active_draft_info(chat_id)
    except Exception:
        # 取不到状态时不要误伤发送，交给死亡标记兜底
        logger.debug("_is_current_active_draft 内部忽略的异常", exc_info=True)
        return True
    if not info:
        return False
    try:
        return int(info[0]) == draft_id_int
    except Exception:
        logger.debug("_is_current_active_draft 内部忽略的异常", exc_info=True)
        return False


async def _reassert_active_draft_content(chat_id: int, draft_id: int) -> None:
    """
    在永久消息发送之后，立刻用缓存的最新草稿内容再推一帧。

    Telegram 客户端在 bot 发出永久消息时会清掉/挤开当前 draft 预览；
    若不立刻 reassert，要等下一次 flush 间隔，用户就会看到
    「列表占了草稿位，草稿稍后在指令下方重新出现」。
    调用方必须已持有该 draft 的 send lock。
    """
    try:
        if await is_draft_dead(draft_id):
            return
        if not await _is_current_active_draft(chat_id, draft_id):
            return
        cache_key = (chat_id, draft_id)
        html_content = _last_sent_draft_cache.get(cache_key)
        if not html_content or not str(html_content).strip():
            return

        payload = {
            "chat_id": chat_id,
            "draft_id": draft_id,
            "rich_message": _rich_message_html_payload(html_content),
        }
        # reassert 只是视觉保活，失败可由下一次真实 flush 恢复；不应占用草稿锁过久。
        session = await get_http_session()
        async with session.post(
            f"{BASE_URL}/sendRichMessageDraft",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=4, connect=2),
        ) as resp:
                if resp.status == 200:
                    _draft_last_send_time[cache_key] = time.monotonic()
                    try:
                        data = await resp.json()
                        msg_id = (data.get("result") or {}).get("message_id")
                        if isinstance(msg_id, int) and msg_id > 0:
                            logger.debug(
                                f"reassert draft ok: chat={chat_id} draft={draft_id} msg_id={msg_id}"
                            )
                    except Exception:
                        logger.debug("_reassert_active_draft_content 内部忽略的异常", exc_info=True)
                        pass
                else:
                    body = ""
                    try:
                        body = await resp.text()
                    except Exception:
                        logger.debug("_reassert_active_draft_content 内部忽略的异常", exc_info=True)
                        pass
                    logger.debug(
                        f"reassert draft failed: chat={chat_id} draft={draft_id} "
                        f"status={resp.status} body={body[:120]}"
                    )
    except Exception as e:
        logger.debug(f"reassert draft exception: chat={chat_id} draft={draft_id} {e}")


@asynccontextmanager
async def serialize_with_active_draft(chat_id: int, *, reassert: bool = True):
    """
    将永久 bot 消息与当前活跃草稿的刷新串行化。

    修复：生成中发送 /model、/role、/balance 等指令回执时，
    sendRichMessage 与 sendRichMessageDraft 并发，客户端会把列表画在
    草稿视觉位，随后迟到的草稿刷新又出现在指令下方。

    持有活跃草稿的 send lock 期间发送永久消息，可保证：
      1) 等在途草稿刷新先结束
      2) 再发列表/确认等永久消息
      3) 可选立刻 reassert 草稿，使其稳定出现在新消息下方继续生成
    """
    draft_id = None
    try:
        from apitelegramchat.state import get_active_draft_info
        info = await get_active_draft_info(chat_id)
        if info:
            draft_id = int(info[0])
    except Exception:
        logger.debug("serialize_with_active_draft 内部忽略的异常", exc_info=True)
        draft_id = None

    if draft_id is None:
        yield
        return

    # 即使草稿已 mark_dead，仍持有 send lock 与在途刷新串行，
    # 避免“最终回复 / 指令列表”与迟到的 draft HTTP 交错。
    # reassert 仅在草稿仍存活时执行。
    lock = await _get_draft_send_lock(chat_id, draft_id)
    async with lock:
        yield
        if reassert:
            try:
                if await is_draft_dead(draft_id):
                    return
            except Exception:
                logger.debug("serialize_with_active_draft 内部忽略的异常", exc_info=True)
                pass
            await _reassert_active_draft_content(chat_id, draft_id)


# ---------- 常规草稿发送（带锁） ----------
async def send_rich_message_draft(
    chat_id: int,
    draft_id,
    html_content: str,
    message_thread_id: Optional[int] = None,
    force: bool = False,
) -> Optional[int]:
    if not html_content or not html_content.strip():
        return 0
    html_content = html_content.strip()
    # 防止 Telegram 返回 400 RICH_MESSAGE_CONTENT_REQUIRED：
    # 只含 HTML 标签（如 <br/>、<b></b>、&nbsp;）但无可见文本时，Telegram 会拒绝。
    # 用一个简单的 tag-stripping 检查：剥掉所有 <xxx> 标签和 HTML 实体后若为空/纯空白，直接跳过。
    _visible_text = re.sub(r'<[^>]+>', ' ', html_content)
    _visible_text = re.sub(r'&[a-zA-Z#0-9]+;', ' ', _visible_text)
    _visible_text = re.sub(r'\s+', ' ', _visible_text).strip()
    if not _visible_text:
        logger.debug(f"send_rich_message_draft: skip empty-after-strip content (len={len(html_content)})")
        return 0
    try:
        draft_id_int = int(draft_id)
        if draft_id_int == 0:
            raise ValueError("draft_id must be non-zero")
    except (ValueError, TypeError) as e:
        logger.error(f"send_rich_message_draft: invalid draft_id={draft_id!r}: {e}")
        return None

    lock = await _get_draft_send_lock(chat_id, draft_id_int)
    async with lock:
        if await is_draft_dead(draft_id_int):
            return 0
        if not await _is_current_active_draft(chat_id, draft_id_int):
            return 0

        cache_key = (chat_id, draft_id_int)
        last_sent = _last_sent_draft_cache.get(cache_key)
        if not force and last_sent == html_content:
            return 0

        if not force:
            last_time = _draft_last_send_time.get(cache_key, 0.0)
            wait_for_slot = _DRAFT_MIN_INTERVAL - (time.monotonic() - last_time)
            if wait_for_slot > 0:
                # 不直接丢弃这次新状态。等待至多 250ms 后发送，避免 builder 把
                # pending_chars 清零、随后只能等静默保活周期才重新显示更新。
                await asyncio.sleep(wait_for_slot)
                if await is_draft_dead(draft_id_int):
                    return 0
                if not await _is_current_active_draft(chat_id, draft_id_int):
                    return 0
                if _last_sent_draft_cache.get(cache_key) == html_content:
                    return 0

        payload = {
            "chat_id": chat_id,
            "draft_id": draft_id_int,
            "rich_message": _rich_message_html_payload(html_content),
        }
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        # 草稿帧可被更晚的完整帧覆盖。把单次等待限制在 5 秒，并至多做一次
        # 短暂重试（按请求传入超时），避免网络抖动时的锁占用造成前端"卡住"。
        for attempt in range(_DRAFT_MAX_ATTEMPTS):
            try:
                session = await get_http_session()
                async with session.post(
                    f"{BASE_URL}/sendRichMessageDraft",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(
                        total=_DRAFT_REQUEST_TIMEOUT,
                        connect=_DRAFT_CONNECT_TIMEOUT,
                    ),
                ) as resp:
                        body = ""
                        if resp.status != 200:
                            try:
                                body = await resp.text()
                            except Exception:
                                logger.debug("send_rich_message_draft 内部忽略的异常", exc_info=True)
                                body = ""

                        if resp.status == 200:
                            _draft_last_send_time[cache_key] = time.monotonic()
                            _last_sent_draft_cache[cache_key] = html_content
                            await _reset_draft_failure(chat_id, draft_id_int)
                            try:
                                data = await resp.json()
                                msg_id = (data.get("result") or {}).get("message_id")
                                if isinstance(msg_id, int) and msg_id > 0:
                                    return msg_id
                            except Exception:
                                logger.debug("send_rich_message_draft 内部忽略的异常", exc_info=True)
                                pass
                            return 0

                        if resp.status == 429:
                            try:
                                data = json.loads(body)
                                retry_after = int(data.get("parameters", {}).get("retry_after", 5))
                            except Exception:
                                logger.debug("send_rich_message_draft 内部忽略的异常", exc_info=True)
                                retry_after = 5
                            raise RateLimitError(retry_after)

                        body_lower = body.lower()
                        hard_not_found = (
                            resp.status in (404, 410)
                            or "not found" in body_lower
                            or "message to edit not found" in body_lower
                        )
                        not_modified = (
                            resp.status == 400 and "message is not modified" in body_lower
                        )

                        if not_modified:
                            _last_sent_draft_cache[cache_key] = html_content
                            await _reset_draft_failure(chat_id, draft_id_int)
                            return 0

                        # RICH_MESSAGE_CONTENT_REQUIRED：内容暂时没有块级元素（<details> 里
                        # 只有纯文本、或空 <details>）。这是流式过程中的瞬态结构问题，
                        # 下一帧 flush 通常会自带块级内容。不刷 WARNING、不累计 failure，
                        # 当作"本帧跳过"处理，避免日志噪音和无谓的 draft 死亡标记。
                        content_required = (
                            resp.status == 400 and "rich_message_content_required" in body_lower
                        )
                        if content_required:
                            logger.debug(
                                f"sendRichMessageDraft skip (RICH_MESSAGE_CONTENT_REQUIRED), "
                                f"will retry on next flush: chat={chat_id} draft={draft_id_int} "
                                f"len={len(html_content)}"
                            )
                            return 0

                        # 媒体抓取失败类错误（RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND /
                        # RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND）是不可恢复的内容问题：
                        # 同一个 URL 再发多少次都会被 Telegram 拒绝。若只 bump
                        # failure 并 return，builder 的 flush 循环会继续用原始内容
                        # 重试，最多累计 6 次失败才 mark dead，用户看到草稿卡很久。
                        # 因此立即在同一个调用内把所有媒体降级为 <a> 链接并重试一次，
                        # 失败一次就直接降级，不让上层循环重复无效请求。
                        media_not_found = (
                            "rich_message_photo_no_media_found" in body_lower
                            or "rich_message_video_no_media_found" in body_lower
                        )
                        if media_not_found:
                            demoted = _demote_all_media_to_links(html_content)
                            if demoted and demoted != html_content:
                                demoted_payload = {
                                    **payload,
                                    "rich_message": _rich_message_html_payload(demoted),
                                }
                                logger.warning(
                                    "sendRichMessageDraft 媒体抓取失败，立即降级为链接重试: "
                                    "chat=%s draft=%s orig_len=%s demoted_len=%s",
                                    chat_id, draft_id_int, len(html_content), len(demoted),
                                )
                                try:
                                    async with session.post(
                                        f"{BASE_URL}/sendRichMessageDraft",
                                        json=demoted_payload,
                                        timeout=aiohttp.ClientTimeout(
                                            total=_DRAFT_REQUEST_TIMEOUT,
                                            connect=_DRAFT_CONNECT_TIMEOUT,
                                        ),
                                    ) as demoted_resp:
                                        if demoted_resp.status == 200:
                                            _draft_last_send_time[cache_key] = time.monotonic()
                                            _last_sent_draft_cache[cache_key] = demoted
                                            await _reset_draft_failure(chat_id, draft_id_int)
                                            try:
                                                demoted_data = await demoted_resp.json()
                                                demoted_msg_id = (demoted_data.get("result") or {}).get("message_id")
                                                if isinstance(demoted_msg_id, int) and demoted_msg_id > 0:
                                                    return demoted_msg_id
                                            except Exception:
                                                logger.debug("send_rich_message_draft 内部忽略的异常", exc_info=True)
                                                pass
                                            return 0
                                        demoted_body = await demoted_resp.text()
                                        logger.warning(
                                            "sendRichMessageDraft 降级后仍失败: %s %s",
                                            demoted_resp.status, demoted_body[:200],
                                        )
                                except Exception as demoted_err:
                                    logger.warning(
                                        "sendRichMessageDraft 降级重试异常: %s", demoted_err,
                                    )

                        # 403 类永久性失败（用户屏蔽 bot 等）：熔断该 chat 的
                        # 主动唤醒调度，并立即判死本草稿——flush 循环继续用
                        # 原内容重试只会无限撞墙，草稿永远出不去。
                        if await _notify_chat_unreachable(chat_id, resp.status, body):
                            await mark_draft_dead(draft_id_int)
                            return 0

                        failures = await _bump_draft_failure(chat_id, draft_id_int)
                        logger.warning(
                            f"sendRichMessageDraft failed (attempt {attempt+1}/{_DRAFT_MAX_ATTEMPTS}, failures={failures}): "
                            f"{resp.status} {body[:200]}"
                        )
                        if hard_not_found and failures >= 5 or failures >= 6:
                            await mark_draft_dead(draft_id_int)
                        return 0

            except RateLimitError:
                raise
            except (aiohttp.ClientConnectorError, asyncio.TimeoutError, aiohttp.ServerDisconnectedError, aiohttp.ClientOSError) as e:
                logger.warning(
                    f"send_rich_message_draft transient error "
                    f"(attempt {attempt + 1}/{_DRAFT_MAX_ATTEMPTS}): {e}"
                )
                if attempt < _DRAFT_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_DRAFT_RETRY_DELAY * (attempt + 1))
                    continue
                failures = await _bump_draft_failure(chat_id, draft_id_int)
                if failures >= 6:
                    await mark_draft_dead(draft_id_int)
                return 0
            except aiohttp.ClientError as e:
                logger.warning(
                    f"send_rich_message_draft client error "
                    f"(attempt {attempt + 1}/{_DRAFT_MAX_ATTEMPTS}): {e}"
                )
                if attempt < _DRAFT_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_DRAFT_RETRY_DELAY * (attempt + 1))
                    continue
                failures = await _bump_draft_failure(chat_id, draft_id_int)
                if failures >= 6:
                    await mark_draft_dead(draft_id_int)
                return 0
            except Exception as e:
                logger.exception(f"send_rich_message_draft unexpected error: {e}")
                failures = await _bump_draft_failure(chat_id, draft_id_int)
                if failures >= 6:
                    await mark_draft_dead(draft_id_int)
                return 0

    return 0

# ---------- 发送普通富文本消息 ----------
# 检测永久消息是否携带 <video> 媒体块：命中时在发送期间显示 upload_video
# （bot 侧“发送视频”动作，与 chat_actions.py 白名单语义一致）。
# 只匹配真正的标签开头，避免误匹配纯文本里的 “<video” 字样或已转义的
# &lt;video；大小写不敏感，兼容自闭合 <video/>。
_VIDEO_TAG_RE = re.compile(r"<video[\s/>]", re.IGNORECASE)


def _rich_html_contains_video(html_content: Optional[str]) -> bool:
    """永久富文本是否携带 <video> 媒体块（用于触发 upload_video 状态）。"""
    try:
        return bool(html_content) and bool(_VIDEO_TAG_RE.search(html_content))
    except Exception:
        logger.debug("_rich_html_contains_video 内部忽略的异常", exc_info=True)
        return False


async def send_rich_html_message(
    chat_id: int,
    html_content: str,
    reply_parameters: Optional[Dict] = None,
    reply_markup: Optional[Dict] = None,
    message_thread_id: Optional[int] = None,
    reassert_draft: bool = False,
) -> int | bool:
    """
    发送永久富文本消息。

    chat action：当消息携带 <video> 媒体块时，发送期间会显示 upload_video
    （bot 正在发送视频；4 秒循环重发，覆盖 Telegram 服务端拉取视频可能
    耗费的数十秒）。草稿刷新（sendRichMessageDraft）不触发任何动作：
    草稿是流式预览，属 typing 语义，且高频刷新会与状态循环互相干扰。

    reassert_draft:
      False — 仅串行发送，不重新挂回草稿。适合绝大多数永久消息，
              例如停止提示、清空确认、错误提示、最终回复等。
      True  — 若该 chat 仍有活跃草稿，则在发送后立刻 reassert 草稿，
              仅在你确实想让草稿继续贴在新消息下方时使用。
    """
    if not html_content or not html_content.strip():
        return False

    # 记录调用方交付给 Telegram 的原始富文本，不对内容做压缩、截断或预览。
    # 保留 strip 之前的版本，便于排查空白、换行和富媒体 URL 在发送前后的差异。
    raw_html_content = html_content
    # INFO 只输出长度与前 200 字符，避免大消息打爆日志。
    logger.info(
        "[%s] Telegram sendRichMessage 原始内容（长度=%s）：%s",
        chat_id,
        len(raw_html_content),
        raw_html_content[:200],
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[%s] Telegram sendRichMessage 完整原始内容（未截断；长度=%s）：\n%s",
            chat_id,
            len(raw_html_content),
            raw_html_content,
        )
    html_content = html_content.strip()

    payload = {
        "chat_id": chat_id,
        "rich_message": _rich_message_html_payload(html_content),
        "disable_notification": False,
        "protect_content": False,
    }
    # 记录实际 HTTP payload 中的完整 HTML；该内容与上方原始 HTML 一致（仅去首尾空白）。
    payload_html_content = payload["rich_message"]["html"]
    logger.info(
        "[%s] Telegram sendRichMessage payload HTML（长度=%s）：%s",
        chat_id,
        len(payload_html_content),
        payload_html_content[:200],
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[%s] Telegram sendRichMessage 完整 payload HTML（未截断；长度=%s）：\n%s",
            chat_id,
            len(payload_html_content),
            payload_html_content,
        )
    if reply_parameters:
        payload["reply_parameters"] = reply_parameters
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id

    # 永久消息需要比草稿更强的送达可靠性，因此保留重试；但不能不设
    # timeout（aiohttp 默认是几分钟级），否则一旦网络抖动或 Telegram 侧
    # 偶发变慢，三次重试 × 每次可能挂到默认超时，会让调用方（草稿滚动）
    # 阻塞数分钟。这里给一个不算激进的有界超时：单次总超时 15s、连接
    # 超时 5s，三次重试封顶约 45~90s（含 1s/4s/7s 退避），同时仍然给
    # 网络抖动足够的恢复空间。
    @retry_async(max_retries=3, delay=1, backoff=3, exceptions=(aiohttp.ClientError, asyncio.TimeoutError))
    async def _send_inner():
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # ---------- 第 1 次尝试：原样发送 ----------
                async with session.post(f"{BASE_URL}/sendRichMessage", json=payload) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            msg_id = (data.get("result") or {}).get("message_id")
                            if isinstance(msg_id, int) and msg_id > 0:
                                return msg_id
                        except Exception as e:
                            logger.debug(f"sendRichHtmlMessage parse response failed: {e}")
                        return True
                    body = await resp.text()
                    body_lower = body.lower()
                    # 只有明确的内容错误才进入针对性兜底。网络错误由装饰器重试，
                    # 认证、权限、限流和参数错误不能靠改 HTML 修复，必须原样失败。
                    if resp.status != 400:
                        # 403 类永久性失败（用户屏蔽 bot / 账号注销 / chat 不
                        # 存在）：重试与降级都救不回来。熔断该 chat 的主动唤
                        # 醒调度，避免 TIMER 每 5~20min 空转一轮完整 LLM
                        # 回合却永远送达不了；用户解除屏蔽后会自动恢复。
                        if await _notify_chat_unreachable(chat_id, resp.status, body):
                            return False
                        logger.error(f"sendRichHtmlMessage failed: {resp.status} {body[:200]}")
                        return False

                    body_lower = body.lower()
                    media_kinds: set[str] = set()
                    if "rich_message_photo_" in body_lower or "rich_message_photo_url_invalid" in body_lower:
                        media_kinds.add("img")
                    if "rich_message_video_" in body_lower or "rich_message_video_url_invalid" in body_lower:
                        media_kinds.add("video")
                    if "rich_message_audio_" in body_lower or "rich_message_audio_url_invalid" in body_lower:
                        media_kinds.add("audio")

                    # ---------- 第 2 次尝试：逐个排查有问题的媒体 ----------
                    # 不再一次性降级所有同类型媒体，而是逐个尝试找出有问题的那个。
                    # 策略：对每个媒体类型，提取所有该类型的媒体，逐个降级测试。
                    if media_kinds:
                        success_result = await _selective_media_fallback(
                            session, BASE_URL, payload, html_content, media_kinds
                        )
                        if success_result:
                            return success_result
                        
                        # 逐个排查失败，最后兜底：降级该类型所有媒体
                        media_demoted = _demote_all_media_to_links(
                            html_content,
                            media_kinds,
                        )
                        if media_demoted and media_demoted != html_content:
                            media_payload = {
                                **payload,
                                "rich_message": _rich_message_html_payload(media_demoted),
                            }
                            logger.warning(
                                "sendRichHtmlMessage retrying with ALL affected media demoted (last resort) "
                                "(kinds=%s, orig_len=%s, demoted_len=%s)",
                                sorted(media_kinds), len(html_content), len(media_demoted),
                            )
                            async with session.post(f"{BASE_URL}/sendRichMessage", json=media_payload) as fb_resp:
                                if fb_resp.status == 200:
                                    try:
                                        fb_data = await fb_resp.json()
                                        fb_msg_id = (fb_data.get("result") or {}).get("message_id")
                                        if isinstance(fb_msg_id, int) and fb_msg_id > 0:
                                            return fb_msg_id
                                    except Exception as e:
                                        logger.debug(f"sendRichHtmlMessage media-demoted parse failed: {e}")
                                    return True
                                fb_body = await fb_resp.text()
                                logger.warning(
                                    "sendRichHtmlMessage all-media fallback failed: %s %s",
                                    fb_resp.status, fb_body[:200],
                                )
                                return False

                    # ---------- 结构/内容错误：保留可见文字，去掉全部富文本标记 ----------
                    # CONTENT_REQUIRED 或未知 Rich Message 400 不是媒体问题，不应把
                    # 无辜的媒体改成链接；纯文本段落是最后一道、语义不丢失的兜底。
                    if "rich_message_content_required" in body_lower or "rich_message_" in body_lower:
                        plain_html = _rich_message_plain_text_fallback(html_content)
                        if plain_html and plain_html != html_content:
                            plain_payload = {
                                **payload,
                                "rich_message": _rich_message_html_payload(plain_html),
                            }
                            logger.warning(
                                "sendRichHtmlMessage retrying with plain-text paragraph fallback "
                                "after content/structure error (orig_len=%s, plain_len=%s)",
                                len(html_content), len(plain_html),
                            )
                            async with session.post(f"{BASE_URL}/sendRichMessage", json=plain_payload) as fb_resp:
                                if fb_resp.status == 200:
                                    try:
                                        fb_data = await fb_resp.json()
                                        fb_msg_id = (fb_data.get("result") or {}).get("message_id")
                                        if isinstance(fb_msg_id, int) and fb_msg_id > 0:
                                            return fb_msg_id
                                    except Exception as e:
                                        logger.debug(f"sendRichHtmlMessage plain fallback parse failed: {e}")
                                    return True
                                fb_body = await fb_resp.text()
                                logger.warning(
                                    "sendRichHtmlMessage plain-text fallback failed: %s %s",
                                    fb_resp.status, fb_body[:200],
                                )
                    logger.error("sendRichHtmlMessage 400 未命中可恢复错误类型: %s %s", resp.status, body[:200])
                    return False
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise
        except Exception:
            logger.exception("sendRichHtmlMessage unexpected exception")
            return False

    # —— chat action：bot 正在发送视频 ——
    # 仅当永久消息携带 <video> 媒体块时触发 upload_video；发送（含内部
    # 至多 3 次重试、媒体降级重试）期间由 chat_actions 的 4 秒循环保活。
    # 周期导入：chat_actions 顶层依赖 utils，这里函数内延迟导入避免循环。
    _video_action = _rich_html_contains_video(html_content)
    if _video_action:
        from apitelegramchat.chat_actions import start_chat_action
        await start_chat_action(chat_id, "upload_video")
    try:
        async with serialize_with_active_draft(chat_id, reassert=reassert_draft):
            return await _send_inner()
    finally:
        if _video_action:
            from apitelegramchat.chat_actions import stop_chat_action
            await stop_chat_action(chat_id, "upload_video")

# ==================== 发送 Chat Action ====================
# 低层原语：直接 POST sendChatAction（单次、无循环）。
# 状态最多持续约 5 秒，长任务必须循环重发——该职责由 chat_actions.py
# 统一承担（4 秒重发循环 + 白名单 + 引用计数）。业务代码请勿直接调用
# 本函数，一律走 apitelegramchat.chat_actions。
async def send_chat_action(chat_id: int, action: str) -> None:
    payload = {"chat_id": chat_id, "action": action}
    # 必须设置超时：此前完全没设 timeout，Telegram API 偶尔 stall 时
    # 会无限期挂起协程，间接阻塞整个 chat 的活跃任务。
    timeout = aiohttp.ClientTimeout(total=5, connect=3)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{BASE_URL}/sendChatAction", json=payload) as resp:
                if resp.status != 200:
                    body = ""
                    try:
                        body = await resp.text()
                    except Exception:
                        pass
                    # 用户屏蔽 bot 时连 typing 指示都会 403：顺手熔断
                    #（幂等，仅标记 + 停调度，不影响本调用返回）。
                    await _notify_chat_unreachable(chat_id, resp.status, body)
                    logger.warning(f"sendChatAction failed: {body[:200]}")
    except Exception as e:
        logger.warning(f"sendChatAction exception: {e}")

# ========== 富消息文本提取 ==========
def _extract_rich_message_text(rich_obj: Union[dict, list, str]) -> str:
    if isinstance(rich_obj, str):
        return rich_obj
    if isinstance(rich_obj, list):
        parts = []
        for item in rich_obj:
            parts.append(_extract_rich_message_text(item))
        return "".join(parts)
    if isinstance(rich_obj, dict):
        block_type = rich_obj.get("type")
        if block_type == "paragraph":
            return _extract_rich_message_text(rich_obj.get("text", ""))
        elif block_type == "list":
            items = rich_obj.get("items", [])
            item_texts = []
            for item in items:
                label = item.get("label", "")
                blocks = item.get("blocks", [])
                content = _extract_rich_message_text(blocks)
                if label:
                    item_texts.append(f"{label} {content}")
                else:
                    item_texts.append(content)
            return "\n".join(item_texts)
        elif block_type in ("bold", "italic", "underline", "strikethrough", "code", "spoiler"):
            return _extract_rich_message_text(rich_obj.get("text", ""))
        else:
            result = []
            for key, value in rich_obj.items():
                if key in ("text", "blocks", "items"):
                    result.append(_extract_rich_message_text(value))
            return "".join(result)
    return ""

def extract_sticker_metadata(sticker: dict) -> dict:
    """从 Telegram Sticker 对象里抽取**对 LLM 有语义价值**的字段。

    Telegram Bot API 的 Sticker 对象字段（已查证
    https://core.telegram.org/bots/api#sticker 与 changelog）很多，
    但大部分对 LLM 没有意义（file_id / file_unique_id 是不透明 ID，
    width / height / file_size 是数字尺寸，thumbnail / 
    premium_animation / mask_position 是几何 / 文件对象），
    LLM 拿到也只是噪声。本函数只保留 LLM 能真正读懂的语义字段。

    ⚠️ 已确认事实（已查证官方文档与 changelog）：
      - Sticker 对象 **没有** `emoji_list` 字段（该字段只在 InputSticker
        上，即 bot 上传贴纸时使用的请求对象）。Sticker 上 emoji 相关
        的字段只有一个：`emoji`（单个字符串，可选）。
      - Sticker 对象 **没有** `format` 字段；格式由 `is_animated` /
        `is_video` 两个布尔表达。本函数按官方说明派生 format=
        static/animated/video 便于 LLM 阅读。

    输出字段（缺字段的直接跳过，不写入字典；只为 LLM 服务的字段）：
      emoji     : str  - Sticker.emoji 原值（唯一的情感语义信号）
      type      : str  - regular / mask / custom_emoji
      format    : str  - 由 is_animated / is_video 派生为
                          static / animated / video
      set_name  : str  - 贴纸包名（如 "AnimatedEmojis" / "Cats"，
                          包名本身常带语义提示）
    """
    if not isinstance(sticker, dict) or not sticker:
        return {}
    meta = {}
    # emoji：Sticker 唯一的 emoji 字段，单个字符串，可选。
    emoji_value = sticker.get("emoji")
    if emoji_value:
        meta["emoji"] = emoji_value
    # type：regular / mask / custom_emoji。
    type_value = sticker.get("type")
    if type_value:
        meta["type"] = type_value
    # set_name：贴纸包名（可空）。
    set_name = sticker.get("set_name")
    if set_name:
        meta["set_name"] = set_name
    # format：派生字段，Sticker 本身没有，由 is_animated / is_video 合成。
    if sticker.get("is_video"):
        meta["format"] = "video"
    elif sticker.get("is_animated"):
        meta["format"] = "animated"
    else:
        meta["format"] = "static"
    return meta


def sticker_metadata_to_text(sticker: dict) -> str:
    """把 Sticker 元数据渲染成对 LLM 友好的短文本。

    用于：用户直接发贴纸 / 引用回复贴纸 / extract_message_text 占位时，
    把贴纸携带的 emoji 等语义信息显式带到对话里，避免 AI 只看到
    "[贴纸]" 这样的无信息占位。

    只输出对 LLM 有语义价值的字段：emoji / 类型 / 格式 / 贴纸包名。
    """
    meta = extract_sticker_metadata(sticker)
    if not meta:
        return "[贴纸]"
    parts = []
    # emoji 优先放在最前，这是 LLM 唯一能直接看见的情感语义信号。
    if meta.get("emoji"):
        parts.append(f"emoji：{meta['emoji']}")
    type_str = meta.get("type")
    if type_str:
        type_label = {
            "regular": "普通贴纸",
            "mask": "面具贴纸",
            "custom_emoji": "自定义表情贴纸",
        }.get(type_str, type_str)
        parts.append(f"类型：{type_label}")
    if meta.get("format"):
        parts.append(f"格式：{meta['format']}")
    if meta.get("set_name"):
        parts.append(f"贴纸包：{meta['set_name']}")
    return "[贴纸] " + " | ".join(parts) if parts else "[贴纸]"


def extract_message_text(message: dict) -> str:
    if not message:
        return ""
    text = message.get("text")
    if text:
        return text
    caption = message.get("caption")
    if caption:
        return caption
    rich = message.get("rich_message")
    if rich:
        if isinstance(rich, str):
            plain = _rich_message_to_text(rich)
            if plain:
                return plain
        elif isinstance(rich, (dict, list)):
            plain = _extract_rich_message_text(rich)
            if plain:
                return plain
    if message.get("photo") or message.get("video") or message.get("audio") or message.get("document"):
        return "[媒体内容]"
    sticker = message.get("sticker")
    if sticker:
        return sticker_metadata_to_text(sticker)
    if message.get("voice"):
        return "[语音消息]"
    content = message.get("content")
    if isinstance(content, str):
        return content
    return ""

def _rich_message_to_text(rich_content: str) -> str:
    if not rich_content:
        return ""
    text = rich_content.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    def replace_table(match):
        table_html = match.group(0)
        rows = re.findall(r'<tr>(.*?)</tr>', table_html, re.DOTALL)
        lines = []
        for row in rows:
            cells = re.findall(r'<t[dh]>(.*?)</t[dh]>', row, re.DOTALL)
            cell_texts = [re.sub(r'<[^>]+>', '', cell).strip() for cell in cells]
            lines.append("| " + " | ".join(cell_texts) + " |")
        return "\n".join(lines)
    text = re.sub(r'<table[^>]*>.*?</table>', replace_table, text, flags=re.DOTALL)
    def replace_list(match):
        list_html = match.group(0)
        if '<ol' in list_html:
            items = re.findall(r'<li>(.*?)</li>', list_html, re.DOTALL)
            numbered = [f"{i+1}. {re.sub(r'<[^>]+>', '', item).strip()}" for i, item in enumerate(items)]
            return "\n".join(numbered)
        else:
            items = re.findall(r'<li>(.*?)</li>', list_html, re.DOTALL)
            bulleted = [f"• {re.sub(r'<[^>]+>', '', item).strip()}" for item in items]
            return "\n".join(bulleted)
    text = re.sub(r'<(ul|ol)[^>]*>.*?</\1>', replace_list, text, flags=re.DOTALL)
    def replace_details(match):
        details = match.group(0)
        summary = re.search(r'<summary>(.*?)</summary>', details, re.DOTALL)
        summary_text = re.sub(r'<[^>]+>', '', summary.group(1)).strip() if summary else "详情"
        content = re.sub(r'<summary>.*?</summary>', '', details, flags=re.DOTALL)
        content_text = re.sub(r'<[^>]+>', '', content).strip()
        return f"[{summary_text}]\n{content_text}" if content_text else f"[{summary_text}]"
    text = re.sub(r'<details[^>]*>.*?</details>', replace_details, text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    return text.strip()

async def transcribe_audio_with_groq(audio_bytes: bytes, file_ext: str = ".ogg") -> str:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY 未设置，无法转录")

    ext_map = {
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4a-latm",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
    }
    content_type = ext_map.get(file_ext.lower(), "audio/ogg")

    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    data = {
        "model": "whisper-large-v3-turbo",
        "language": "zh",
        "response_format": "json"
    }

    form = aiohttp.FormData()
    form.add_field(
        "file",
        audio_bytes,
        filename=f"audio{file_ext}",
        content_type=content_type
    )
    for key, value in data.items():
        form.add_field(key, str(value))

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=form, timeout=30) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise Exception(f"Groq 转录失败 (HTTP {resp.status}): {err_text[:200]}")
            result = await resp.json()
            return result.get("text", "").strip()
