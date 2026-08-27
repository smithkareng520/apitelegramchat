"""工具调用的摘要/描述生成、参数解析与失败判定。

从 ai_handlers.py 拆分而来，逻辑未做改动。
"""
import json
import re
from typing import Optional, Any

from apitelegramchat.utils import get_logger
from apitelegramchat.tool_executors import _TOOL_TIMEOUT_MARKER
from apitelegramchat.ai._constants import MAX_TOOL_CALLS
from apitelegramchat.ai.error_formatting import extract_domain

logger = get_logger(__name__)

# 流式调用偶发截断/拼接异常时，不能把原始坏字符串写回下一轮请求，否则网关会在
# 模型开始生成前直接 400。此标记本身是合法 JSON，并让执行层返回可恢复错误。
_INVALID_TOOL_ARGUMENTS_KEY = "__apitelegram_invalid_tool_arguments__"
_INVALID_TOOL_ARGUMENTS_RAW_KEY = "raw_arguments_excerpt"

_TEXTUAL_TOOL_CALL_RE = re.compile(
    r"<(?:longcat_)?tool_call\b[^>]*>.*?(?:</(?:longcat_)?tool_call\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)

def _get_tool_description_from_args(fn_args: dict) -> Optional[str]:
    """从工具参数中获取简短描述（优先使用 _description，其次 _summary）"""
    if not fn_args:
        return None
    desc = fn_args.get("_description") or fn_args.get("_summary")
    if desc and isinstance(desc, str):
        desc = desc.strip()
        if len(desc) > 80:
            desc = desc[:80] + "..."
        return desc
    return None


def _coerce_positive_int(value: Any, default: int = 1) -> int:
    try:
        num = int(value)
        return num if num > 0 else default
    except (TypeError, ValueError):
        return default


def _extract_web_search_result_count(result_content: Any) -> Optional[int]:
    """Extract the authoritative successful-result count from the search envelope."""
    if result_content is None:
        return None
    if isinstance(result_content, dict):
        for key in ("count", "result_count", "success_count"):
            try:
                value = result_content.get(key)
                if value is not None and int(value) >= 0:
                    return int(value)
            except (TypeError, ValueError):
                pass
        for key in ("results", "items", "search_results", "organic_results"):
            value = result_content.get(key)
            if isinstance(value, list):
                return len(value)
    text = str(result_content).strip()
    if not text:
        return None
    m = re.search(r'\[成功:[^\]]+\].*?[（(]\s*(\d+)\s*/\s*(\d+)\s*[）)]', text, re.S)
    if m:
        return int(m.group(1))
    for pattern in (
        r'Found\s+(\d+)\s+results?',
        r'(\d+)\s+results?\s+found',
        r'共有\s*(\d+)\s*(?:条|个)?\s*结果',
        r'找到\s*(\d+)\s*(?:条|个)?\s*结果',
    ):
        m = re.search(pattern, text, re.I)
        if m:
            return int(m.group(1))
    numbered = re.findall(r'(?m)^\s*(\d{1,3})[.、)、]\s+\S', text)
    if numbered:
        nums = [int(n) for n in numbered]
        if nums and max(nums) <= 50 and len(set(nums)) == max(nums):
            return max(nums)
    return None


def _generate_initial_tool_summary(fn_name: str, fn_args: dict) -> str:
    """
    生成单个工具进行时的摘要（执行中）。
    优先使用自定义 _description，否则按照规范显示固定进行时文本。
    """
    fn_args = fn_args or {}

    # web_search 单工具进行态固定显示搜索词。
    if fn_name == "web_search":
        query = (fn_args.get("query") or "").strip()
        return query if query else "Searching the web"

    custom_desc = _get_tool_description_from_args(fn_args)
    if custom_desc:
        return custom_desc

    # ---------- 特殊处理 ----------

    if fn_name == "fetch_url":
        url = (fn_args.get("url") or "").strip()
        domain = extract_domain(url) if url else ""
        return f"Fetching from {domain}" if domain else "Fetching a page"

    if fn_name == "bash":
        cmd = (fn_args.get("command") or "").strip()
        if cmd:
            short_cmd = cmd[:30] + "..." if len(cmd) > 30 else cmd
            return short_cmd
        return "Running command"

    # ---------- text_editor ----------
    if fn_name == "text_editor":
        command = fn_args.get("command", "")
        path = fn_args.get("path", "")
        # 进行时只显示描述，不需要详细路径
        return custom_desc or {
            "view": "Viewing file",
            "create": "Creating file",
            "str_replace": "Replacing exact text",
            "insert": "Inserting text",
        }.get(command, "Editing file")

    # ---------- 图片类 ----------
    if fn_name == "generate_image_from_text":
        num_images = _coerce_positive_int(fn_args.get("num_images"), 1)
        if num_images == 1:
            return "Generating an image"
        return f"Generating {num_images} images"

    if fn_name == "edit_image_with_reference":
        num_images = _coerce_positive_int(fn_args.get("num_images"), 1)
        if num_images == 1:
            return "Editing an image"
        return f"Editing {num_images} images"

    if fn_name == "generate_video":
        return "Generating a video"

    if fn_name == "ask_user":
        return "Waiting for your answer"

    # ---------- 其他工具，按规范进行时文本 ----------
    mapping = {
        "present_files": "Presenting file(s)",
        "wikipedia": "Looking up on Wikipedia",
        "news": "Fetching news",
        "book_lookup": "Looking up a book",
        "geocode": "Geocoding address",
        "route": "Planning route",
        "distance": "Measuring distance",
        "poi_keyword_search": "Searching POI by keyword",
        "poi_nearby_search": "Searching nearby POI",
        "poi_details": "Fetching POI details",
        "exchange_rate": "Checking exchange rates",
        "crypto_price": "Fetching crypto prices",
        "weather": "Fetching weather",
        "qr_code": "Generating QR code",
    }
    return mapping.get(fn_name, "Running...")


def _generate_action_description(fn_name: str, fn_args: dict = None) -> str:
    """生成动作描述（用于 fallback）"""
    fn_args = fn_args or {}

    custom_desc = _get_tool_description_from_args(fn_args)
    if custom_desc:
        return custom_desc

    if fn_name == "text_editor":
        cmd = fn_args.get("command", "")
        return {
            "view": "viewed a file",
            "create": "created a file",
            "str_replace": "replaced exact text in a file",
            "insert": "inserted text into a file",
        }.get(cmd, "edited a file")

    mapping = {
        "web_search": "searched the web",
        "fetch_url": "fetched a page",
        "wikipedia": "looked up Wikipedia",
        "exchange_rate": "checked exchange rates",
        "book_lookup": "looked up a book",
        "weather": "fetched weather",
        "news": "fetched news",
        "crypto_price": "checked crypto prices",
        "qr_code": "generated a QR code",
        "generate_video": "generated a video",
        "geocode": "geocoded an address",
        "poi_keyword_search": "searched for points of interest by keyword",
        "poi_nearby_search": "searched for nearby points of interest",
        "poi_details": "fetched POI details",
        "route": "planned a route",
        "distance": "measured a distance",
        "bash": "ran a command",
        "present_files": "presented files",
        "ask_user": "asked for your input",
    }
    return mapping.get(fn_name, f"ran {fn_name}")


def _contains_textual_tool_call(content: str) -> bool:
    return bool(content and re.search(r"<(?:longcat_)?tool_call\b", content, re.IGNORECASE))


def _strip_textual_tool_calls(content: str) -> str:
    """移除模型误以纯文本输出的 function-call XML，防止其泄漏到最终用户消息。"""
    if not content:
        return ""
    return _TEXTUAL_TOOL_CALL_RE.sub("", content).strip()


def _tool_limit_summary() -> str:
    return (
        f"本轮已完成 {MAX_TOOL_CALLS} 次工具调用，已达到单轮安全上限。"
        "我已保留成功结果；如仍需继续执行剩余步骤，请发送“继续”。"
    )


def _safe_parse_args(args_str: str) -> dict:
    if not args_str:
        return {}
    try:
        parsed = json.loads(args_str)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    # 流式不完整时，用正则兜底提取 _description
    desc_match = re.search(r'"_description"\s*:\s*"((?:[^"\\]|\\.)*)"', args_str)
    if desc_match:
        # 修复：原代码用三个 .replace() 手工反转义，遗漏 \\u、\\r、\\\\、\\/ 等，
        # 对 `C:\\path` 这样的输入会丢一个反斜杠。改成把正则捕获的字符串
        # 当作 JSON 字符串字面量解析，让 json 模块处理全部转义序列。
        try:
            desc = json.loads(f'"{desc_match.group(1)}"')
            return {"_description": desc}
        except (json.JSONDecodeError, ValueError):
            # 兜底：如果 json.loads 失败，退回到旧的简单反转义。
            desc = desc_match.group(1).replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
            return {"_description": desc}
    return {}


def _normalize_tool_arguments(arguments: Any) -> tuple[str, bool]:
    """返回可安全回传给 provider 的 JSON object 字符串及是否发生规范化。"""
    raw = arguments if isinstance(arguments, str) else str(arguments or "")
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            # 重新序列化同时移除无意义空白，确保所有兼容端收到相同的合法 JSON。
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), False
        reason = "arguments must be a JSON object"
    except (json.JSONDecodeError, TypeError, ValueError):
        reason = "arguments were not valid JSON"

    safe_excerpt = raw[:2000]
    normalized = {
        _INVALID_TOOL_ARGUMENTS_KEY: reason,
        _INVALID_TOOL_ARGUMENTS_RAW_KEY: safe_excerpt,
    }
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":")), True


def _normalize_tool_call_arguments(tool_calls: list[dict], api_label: str, round_number: int) -> int:
    """就地规范化一个模型返回中的所有工具参数，并返回修复数量。"""
    corrected = 0
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        function = tc.setdefault("function", {})
        if not isinstance(function, dict):
            tc["function"] = function = {}
        normalized, was_corrected = _normalize_tool_arguments(function.get("arguments", ""))
        function["arguments"] = normalized
        if was_corrected:
            corrected += 1
    if corrected:
        logger.warning(
            "[%s] 第 %s 轮检测到 %s 个非法工具参数 JSON，已写入可恢复错误并阻止其污染下一轮请求",
            api_label, round_number, corrected,
        )
    return corrected


def _tool_result_is_failure(fn_name: str, fn_args: dict, result_content: Any, details_html: str = "") -> bool:
    """统一判断工具是否失败；失败项不会进入工具组成功统计。"""
    if result_content == _TOOL_TIMEOUT_MARKER:
        return True
    text = str(result_content or "").strip()
    lower = text.lower()
    if fn_name == "bash":
        # bash 的成功/失败以退出码为准；仅在明确看不到退出码时，再回退到错误前缀判断。
        m = re.search(r"Exit code:\s*(\d+)", text)
        if m:
            return m.group(1) != "0"
        if lower.startswith(("error:", "exception:", "failed:", "timeout:", "❌")):
            return True
        return False
    if lower.startswith(("error:", "exception:", "failed:", "timeout:", "❌", "失败：", "失败:")):
        return True
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and payload.get("error"):
                return True
        except Exception:
            pass
    return False


def _generate_tool_summary_done(fn_name: str, fn_args: dict, result_content: str) -> str:
    """生成当前工具完成后的用户可见摘要。"""
    fn_args = fn_args or {}

    if fn_name == "web_search":
        query = (fn_args.get("query") or "").strip()
        count = _extract_web_search_result_count(result_content)
        if query and count is not None:
            return f"{query} {count} result" if count == 1 else f"{query} {count} results"
        return "Searched the web"

    custom_desc = _get_tool_description_from_args(fn_args)
    if custom_desc:
        return custom_desc

    if fn_name == "fetch_url":
        url = (fn_args.get("url") or "").strip()
        domain = extract_domain(url) if url else ""
        text = str(result_content or "").strip()
        if _tool_result_is_failure(fn_name, fn_args, result_content):
            return f"Failed to fetch {domain}" if domain else "Failed to fetch page"
        title = None
        # 新版 fetch_url 结果为 Telegram Rich HTML，标题在 <h3>…</h3>。
        m = re.search(r"<h3[^>]*>(.*?)</h3>", text, re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if not title:
            # 旧格式兼容：🏷️ 标记行。
            m = re.search(r"🏷️\s+([^\n]+)", text)
            if m:
                title = m.group(1).strip()
        if not title:
            m = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
            if m:
                title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                title = re.sub(r"\s+", " ", title)
        return f"Fetched: {title}" if title else (f"Fetched: {domain}" if domain else "Fetched a page")

    if fn_name == "wikipedia":
        query = (fn_args.get("query") or "").strip()
        text = str(result_content or "").strip()
        if _tool_result_is_failure(fn_name, fn_args, result_content):
            return f"Failed to look up {query}" if query else "Failed to look up on Wikipedia"
        # 新版结果为 Telegram Rich HTML，标题在 <h3>…</h3>；
        # 退化路径（纯文本摘要）为 <b>Wikipedia — 标题</b>。
        title = None
        m = re.search(r"<h3[^>]*>(.*?)</h3>", text, re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if not title:
            m = re.search(r"<b>Wikipedia\s*[—-]\s*(.+?)</b>", text, re.S)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()
        if not title:
            title = query
        return f"Looked up: {title}" if title else "Looked up on Wikipedia"

    if fn_name == "ask_user":
        try:
            payload = json.loads(str(result_content or "{}"))
            if payload.get("type") == "choice":
                labels = [str(x.get("label", "")) for x in (payload.get("selected") or []) if isinstance(x, dict)]
                return "Selected: " + ", ".join([x for x in labels if x][:3]) if labels else "User answered"
            if payload.get("type") == "custom":
                return "User provided a custom answer"
            if payload.get("type") == "cancelled":
                return "User cancelled"
            if payload.get("type") == "expired":
                return "User answer expired"
        except Exception:
            pass
        return "User answered"

    if fn_name == "bash":
        return "Ran a command"

    if fn_name == "text_editor":
        return {
            "view": "Viewed a file",
            "create": "Created a file",
            "str_replace": "Replaced exact text in a file",
            "insert": "Inserted text into a file",
        }.get(fn_args.get("command", ""), "Edited a file")

    if fn_name == "present_files":
        paths = fn_args.get("paths", [])
        n = len(paths) if isinstance(paths, list) else 0
        return "Presented file" if n <= 1 else f"Presented {n} files"

    if fn_name == "generate_image_from_text":
        n = _coerce_positive_int(fn_args.get("num_images"), 1)
        return "Generated an image" if n == 1 else f"Generated {n} images"
    if fn_name == "edit_image_with_reference":
        n = _coerce_positive_int(fn_args.get("num_images"), 1)
        return "Edited an image" if n == 1 else f"Edited {n} images"
    if fn_name == "generate_video":
        return "Generated a video"
    if fn_name == "qr_code":
        return "Generated a QR code"

    mapping = {
        "wikipedia": "Looked up on Wikipedia",
        "news": "Fetched news",
        "book_lookup": "Looked up a book",
        "geocode": "Geocoded an address",
        "nearby_search": "Searched nearby",
        "route": "Planned a route",
        "distance": "Measured a distance",
        "poi_keyword_search": "Searched POIs by keyword",
        "poi_nearby_search": "Searched nearby POIs",
        "poi_details": "Fetched POI details",
        "exchange_rate": "Checked exchange rates",
        "crypto_price": "Fetched crypto prices",
        "public_holidays": "Looked up holidays",
        "weather": "Fetched weather",
        "convert": "Calculated a result",
    }
    return mapping.get(fn_name, "Ran an action")


