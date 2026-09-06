"""API 错误信息解析与用户可读的错误提示格式化。

从 ai_handlers.py 拆分而来，逻辑未做改动。
"""
import ast
import io
import json
import re
import html
from typing import Optional, Any
from urllib.parse import urlparse
from PIL import Image

from apitelegramchat.utils import strip_html_tags, escape_html, get_logger

logger = get_logger(__name__)

# 改为 frozenset：避免误操作修改；查询性能更好（O(1) 包含判定）。
# 同时预计算 lower-case 版本（中文无需小写，但英文要），避免在热路径
# 上反复 .lower()。
_CONTENT_SAFETY_KEYWORDS_RAW = (
    'inappropriate content',
    'content filter',
    'safety filter',
    'nsfw',
    'sensitive content',
    'blocked by safety',
    'violates policy',
    'violates our policy',
    '敏感内容',
    '不当内容',
    '违规内容',
    '安全限制',
    '内容审核',
)
_CONTENT_SAFETY_KEYWORDS = frozenset(
    kw if any('\u4e00' <= ch <= '\u9fff' for ch in kw) else kw.lower()
    for kw in _CONTENT_SAFETY_KEYWORDS_RAW
)

def _strip_prefix_error_message(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if cleaned.startswith("⚠️ "):
        cleaned = cleaned[2:].strip()
    # 常见 SDK/HTTP 客户端会把真正的报错放在 message='...'
    # 这一段里，先把外层包装去掉，便于后续 JSON 解析与摘要提取。
    m = re.search(r'message\s*=\s*([\'"])(.*?)(?:\1(?:,|$)|$)', cleaned, re.S)
    if m:
        cleaned = m.group(2).strip()
    if " - {" in cleaned:
        cleaned = cleaned.split(" - ", 1)[1].strip()
    return cleaned


def extract_error_body_text(exception: Exception) -> str:
    """从异常对象中安全提取上游响应 body 文本；任何情况下都不抛异常。

    背景（2026-09 生产事故）：anthropic / openai SDK 抛出的
    APIStatusError 携带的 e.response 是 httpx.Response。流式请求场景下
    响应体尚未被读取，直接访问 ``e.response.text`` 属性会抛
    httpx.ResponseNotRead——而 Python 的 ``hasattr()`` 只吞
    AttributeError，导致该异常从「错误处理代码」本身逃逸，
    把真正的上游错误（如 503 overloaded）完全掩盖，用户只能看到
    "Attempted to access streaming response content..." 这句废话。

    提取优先级：
    1. ``e.body``：SDK 已经解析好的响应体（dict / str），首选，
       完全不依赖 httpx 的读取状态；dict 形态转回 JSON 文本。
    2. ``e.response.text``：仅对已读取的非流式响应有效（openai SDK
       在构造状态错误前会 aread()，此路径可用）；流式未读时抛
       ResponseNotRead，此处用 try/except 兜住，静默放弃。
    3. 都取不到时返回 ""，调用方自行回退到 str(e)。
    """
    if exception is None:
        return ""

    # ---- 优先级 1：SDK 解析好的 body ----
    body_obj = getattr(exception, "body", None)
    if isinstance(body_obj, str) and body_obj.strip():
        return body_obj
    if body_obj is not None:
        try:
            return json.dumps(body_obj, ensure_ascii=False)
        except Exception:
            logger.debug("extract_error_body_text: body 序列化失败", exc_info=True)

    # ---- 优先级 2：httpx 响应文本（仅已读取时可用） ----
    response = getattr(exception, "response", None)
    if response is not None:
        try:
            text = response.text  # 流式未读时此处抛 ResponseNotRead
            if isinstance(text, str):
                return text
        except Exception:
            logger.debug(
                "extract_error_body_text: response.text 不可读（流式响应未消费），跳过",
                exc_info=True,
            )

    return ""


def _coerce_error_payload(payload_text: str) -> Any:
    """尽量把错误文本还原成 dict/list，便于抽取关键信息。"""
    if not payload_text:
        return None
    text = _strip_prefix_error_message(payload_text).strip()
    if not text:
        return None

    candidates = [text]
    if "{" in text:
        candidates.append(text[text.find("{"):].strip())
    if "[" in text:
        candidates.append(text[text.find("["):].strip())

    for blob in candidates:
        blob = blob.strip()
        if not blob:
            continue
        if not (blob.startswith(("{", "["))):
            continue
        try:
            return json.loads(blob)
        except Exception:
            logger.debug("_coerce_error_payload 内部忽略的异常", exc_info=True)
            try:
                return ast.literal_eval(blob)
            except Exception:
                logger.debug("_coerce_error_payload 内部忽略的异常", exc_info=True)
                continue
    return None


def _extract_detail_lines_from_payload(payload: Any) -> list[str]:
    lines: list[str] = []

    def _push(label: str, value: Any):
        if value is None:
            return
        if isinstance(value, str):
            value = re.sub(r"\s+", " ", value.strip())
        if value == "":
            return
        lines.append(f"{label}：{value}")

    def _walk(obj: Any):
        if obj is None:
            return
        if isinstance(obj, list):
            for item in obj[:5]:
                _walk(item)
            return
        if not isinstance(obj, dict):
            _push("详情", obj)
            return

        # 常见结构：{"error": {...}}
        err = obj.get("error")
        if isinstance(err, dict):
            obj = err
        elif isinstance(err, str):
            _push("消息", err)

        # 基础字段
        for key, label in (
            ("code", "代码"),
            ("status", "状态"),
            ("message", "消息"),
            ("detail", "详情"),
            ("request_id", "Request ID"),
            ("requestId", "Request ID"),
            ("type", "类型"),
        ):
            _push(label, obj.get(key))

        # Gemini / Google 风格的配额与帮助信息
        details = obj.get("details")
        if isinstance(details, list):
            for item in details[:5]:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("@type") or "")
                if "QuotaFailure" in item_type:
                    violations = item.get("violations")
                    if isinstance(violations, list):
                        for v in violations[:5]:
                            if not isinstance(v, dict):
                                continue
                            _push("配额指标", v.get("quotaMetric"))
                            _push("限制", v.get("limit"))
                            _push("主体", v.get("subject"))
                            _push("说明", v.get("description"))
                elif "Help" in item_type:
                    links = item.get("links")
                    if isinstance(links, list) and links:
                        first = links[0]
                        if isinstance(first, dict):
                            _push("帮助", first.get("description"))
                            _push("链接", first.get("url"))

        # 兜底：把少量有用字段也列出来
        for key in ("quotaMetric", "limit", "subject", "description", "retryAfter", "retry_after"):
            if key in obj:
                label = {
                    "quotaMetric": "配额指标",
                    "limit": "限制",
                    "subject": "主体",
                    "description": "说明",
                    "retryAfter": "重试等待",
                    "retry_after": "重试等待",
                }.get(key, key)
                _push(label, obj.get(key))

        # 有些 JSON 里把真正信息塞在 message 里，顺手把常见的 retry 提示补出来
        message = str(obj.get("message") or obj.get("detail") or "")
        m = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", message, re.I)
        if m:
            _push("建议重试", f"{m.group(1)}s 后重试")

    _walk(payload)

    # 去重并保留顺序
    deduped: list[str] = []
    seen: set[str] = set()
    for line in lines:
        norm = line.strip()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        deduped.append(norm)
    return deduped


def _extract_error_details(error_message: str = "", exception: Optional[Exception] = None) -> tuple[str, str]:
    """从原始异常文本中提取更适合展示的错误摘要与 request_id。"""
    chunks: list[str] = []
    for item in (error_message, exception):
        if not item:
            continue
        try:
            chunks.append(str(item))
        except Exception:
            logger.debug("_extract_error_details 内部忽略的异常", exc_info=True)
            continue

    raw_text = "\n".join(part for part in chunks if part).strip()
    if not raw_text:
        return "", ""

    # 先尝试把外层包装剥掉，再解析 JSON / Python 字面量。
    cleaned = _strip_prefix_error_message(raw_text)
    payload = _coerce_error_payload(cleaned)
    if payload is None and cleaned != raw_text:
        payload = _coerce_error_payload(raw_text)

    if payload is not None:
        # 尽量从 payload 里提取 request_id
        request_id = ""

        def _find_request_id(obj: Any) -> str:
            if isinstance(obj, dict):
                for key in ("request_id", "requestId", "requestID", "x-request-id", "x_request_id"):
                    value = obj.get(key)
                    if value:
                        return str(value).strip()
                for value in obj.values():
                    found = _find_request_id(value)
                    if found:
                        return found
            elif isinstance(obj, list):
                for item in obj[:10]:
                    found = _find_request_id(item)
                    if found:
                        return found
            return ""

        request_id = _find_request_id(payload)
        lines = _extract_detail_lines_from_payload(payload)
        if lines:
            return "\n".join(lines), request_id

    # 兜底：把常见的转义换行展开，保留少量有用内容。
    fallback = cleaned.replace("\\r\\n", "\n").replace("\\r", "\n").replace("\\n", "\n")
    lines = [line.strip() for line in fallback.splitlines() if line.strip()]
    if not lines:
        return "", ""

    # 只保留前几行，避免把超长原始响应全打出来。
    trimmed = lines[:12]
    return "\n".join(trimmed), ""


def _format_error_detail_for_display(detail: str) -> str:
    """把原始错误详情转成更适合聊天窗口阅读的 HTML 文本。"""
    if not detail:
        return ""
    clean = strip_html_tags(str(detail)).strip()
    if not clean:
        return ""

    payload = _coerce_error_payload(clean)
    if payload is not None:
        lines = _extract_detail_lines_from_payload(payload)
        if lines:
            return "<br/>".join(escape_html(line) for line in lines)

    # fallback：按行输出，先把转义序列恢复成可读文本
    clean = clean.replace("\\r\\n", "\\n").replace("\\r", "\\n").replace("\\n", "\n")
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    if not lines:
        return ""
    return "<br/>".join(escape_html(line) for line in lines)


def _format_api_error_notice(
        *,
        api_name: str,
        error_code: int = 0,
        endpoint: str = "",
        model: str = "",
        detail: str = "",
        request_id: str = "",
) -> str:
    parts = [f"⚠️ <b>{escape_html(api_name)} 请求失败</b>"]
    if error_code:
        parts.append(f"HTTP 状态：{error_code}")
    if model:
        parts.append(f"模型：{escape_html(model)}")
    if request_id:
        parts.append(f"Request ID：{escape_html(request_id)}")
    if detail:
        formatted_detail = _format_error_detail_for_display(detail)
        if formatted_detail:
            parts.append(f"详情：{formatted_detail}")
    return "<br/>".join(parts)


def _is_content_safety_error(detail: str) -> bool:
    """检测错误详情是否属于内容安全/审核类（而非技术故障）。

    这类错误通常是因为 prompt 或生成结果触发了模型的内容审核机制，
    不是代码 bug，也不需要展示技术细节（HTTP 状态/端点/Request ID）。
    """
    if not detail:
        return False
    text = detail.lower()
    # _CONTENT_SAFETY_KEYWORDS 已在模块级预计算成小写 frozenset，
    # 此处无需在每个调用上再对每个 kw 调 .lower()。
    return any(kw in text for kw in _CONTENT_SAFETY_KEYWORDS)


def _format_image_safety_notice(detail: str = "", model: str = "") -> str:
    """生成对内容安全错误的友好提示（不包含技术调试信息）。

    用户看到的是：
      ⚠️ 这张图触发了安全限制
      模型检测到提示词或生成结果可能包含不当内容。
      请修改描述后重试，或换一个更中性的表达。
      模型：Z Image Turbo
    """
    parts = ["⚠️ <b>这张图触发了安全限制</b>"]
    parts.append("模型检测到提示词或生成结果可能包含不当内容。")
    parts.append("请修改描述后重试，或换一个更中性的表达。")
    if model:
        parts.append(f"模型：{escape_html(_short_model_name(model))}")
    if detail:
        clean_detail = strip_html_tags(detail).strip()
        if clean_detail and len(clean_detail) < 500:
            parts.append(f"<i>详情：{escape_html(clean_detail)}</i>")
    return "<br/>".join(parts)


def _render_media_failure_quote(error_notice: str) -> str:
    """把原生媒体模型的失败通知渲染成与 text_editor 相同的等宽结果块。

    与 ``tool_executors._render_editor_quote`` 保持同一形态：``<pre><code>``
    而非 ``<blockquote>``。上游报错常带缩进的 JSON / traceback，引用块会把
    空白折叠掉、用比例字体排版，导致结构不可读；``<pre>`` 逐字保留空白。
    """
    raw = html.unescape(str(error_notice or ""))
    visible_text = strip_html_tags(raw).strip()
    if not visible_text:
        visible_text = "媒体生成未完成，请稍后重试。"
    lines = visible_text.splitlines()
    if len(lines) > 20:
        visible_text = "\n".join(lines[:20]) + f"\n…（已截断，共 {len(lines)} 行，仅显示前 20 行）"
    # 严格转义（& 无条件转义）：这是程序原始输出，不是 HTML 片段。
    body = visible_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<p><b>Result</b></p><pre><code>{body}</code></pre>"


def _short_model_name(model: str) -> str:
    """把模型 ID 美化为展示名。
    例：
      'Qwen/Qwen-Image-Edit-2511'      → 'Qwen Image Edit 2511'
      'Tongyi-MAI/Z-Image-Turbo'       → 'Z Image Turbo'
      'bytedance-seed/seedream-4.5'    → 'Seedream 4.5'
      'google/gemini-3-pro-image-preview' → 'Gemini 3 Pro Image Preview'
    """
    if not model:
        return ''
    # 去掉 provider 前缀（"Qwen/..."、"google/..." 等）
    name = model.split('/', 1)[-1]
    # 去掉常见的前缀重复（"Qwen-Image-Edit" 里的 "Qwen-" 当 provider 也是 Qwen 时）
    # 把连字符替换为空格，便于阅读
    name = name.replace('-', ' ').replace('_', ' ')
    # 压缩多余空格
    name = ' '.join(name.split())
    return name


def _format_image_metadata_caption(img_bytes: bytes, model: str) -> str:
    """根据图片字节生成元数据 caption，格式如：
        PNG 760×1280 RGB 1137.4KB · Z Image Turbo
    若 PIL 解析失败，退化为只显示文件大小和模型名。
    """
    model_name = _short_model_name(model)
    size_kb = len(img_bytes) / 1024.0
    # 智能选择单位：< 1024 KB 用 KB，否则用 MB
    if size_kb < 1024:
        size_str = f"{size_kb:.1f}KB"
    else:
        size_str = f"{size_kb / 1024:.2f}MB"

    parts: list[str] = []
    fmt = ''
    try:
        # io.BytesIO 已在模块顶部 import；PIL Image 必须用 with 关闭，
        # 否则反复打开会泄露文件描述符。
        with Image.open(io.BytesIO(img_bytes)) as img:
            fmt = (img.format or '').upper() or 'IMG'
            w, h = img.size
            mode = img.mode or ''
            # RGB / RGBA / L / P 等，只取常见模式的简写
            mode_display = mode if mode in ('RGB', 'RGBA', 'L', 'LA', 'P') else ''
            parts.append(fmt)
            parts.append(f"{w}×{h}")
            if mode_display:
                parts.append(mode_display)
    except Exception as e:
        # 提升到 warning：图片元数据解析失败会让 caption 缺字段，但
        # debug 级别在生产环境几乎不会被打开，问题会被静默吞掉。
        logger.warning(f"[NativeImage] PIL 解析图片元数据失败，退化展示: {e}")
        parts.append('IMG')

    parts.append(size_str)
    caption = ' '.join(parts)
    if model_name:
        caption += f" · {model_name}"
    return caption


def _format_video_metadata_caption(
        *,
        file_size_bytes: int,
        model: str,
        meta: Optional[dict] = None,
) -> str:
    """根据视频字节大小和轮询返回的元数据生成 caption，格式如：
        MP4 1088×832 24fps 121帧 788.5KB · Agnes Video V2.0
    若没有元数据，退化为：MP4 788.5KB · Agnes Video V2.0
    与 _format_image_metadata_caption 保持同一套视觉风格。
    """
    model_name = _short_model_name(model)
    size_kb = (file_size_bytes or 0) / 1024.0
    if size_kb < 1024:
        size_str = f"{size_kb:.1f}KB"
    else:
        size_str = f"{size_kb / 1024:.2f}MB"

    parts: list[str] = ["MP4"]
    if meta:
        width = meta.get("width")
        height = meta.get("height")
        frame_rate = meta.get("frame_rate")
        num_frames = meta.get("num_frames")
        if width and height:
            parts.append(f"{width}×{height}")
        if frame_rate:
            parts.append(f"{frame_rate}fps")
        if num_frames:
            parts.append(f"{num_frames}帧")
    parts.append(size_str)
    caption = ' '.join(parts)
    if model_name:
        caption += f" · {model_name}"
    return caption


async def get_error_notification_message(
        chat_id: int,
        error_code: int = 0,
        error_message: str = "",
        api_name: str = "API",
        exception: Optional[Exception] = None,
        endpoint: str = "",
        model: str = "",
) -> str:
    """
    不做错误映射，只把原始错误包装成更易读的结构化消息。
    """
    raw_detail, request_id = _extract_error_details(error_message, exception)
    return _format_api_error_notice(
        api_name=api_name,
        error_code=error_code,
        endpoint=endpoint,
        model=model,
        detail=raw_detail,
        request_id=request_id,
    )


def extract_domain(url: str) -> str:
    if not url:
        return "unknown"
    parsed = urlparse(url)
    return parsed.netloc or parsed.path.split('/')[0]


