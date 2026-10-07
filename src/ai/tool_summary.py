"""工具调用的摘要/描述生成、参数解析与失败判定。

从 ai_handlers.py 拆分而来。v2.3 起参数规范化接入 json_repair 的自动修复
与精准诊断（Self-Correction 增强）：修复成功直接用修复后的参数执行工具，
省掉一整轮模型重试；修复失败则把解析器报错/位置/病因写进可恢复信封，
由执行层渲染成给模型的定向修复指引。
"""
import json
import re
from typing import Any, Optional, cast

from utils import get_logger
from tool_executors import _TOOL_TIMEOUT_MARKER
from tool_names import split_mcp_name
from ai._constants import MAX_TOOL_CALLS
from core.text_utils import extract_domain
from ai.json_repair import (
    _INVALID_TOOL_ARGUMENTS_KEY,
    _JSON_REPAIR_NOTE_KEY,
    _finish_reason_cut_info,
    build_invalid_arguments_envelope,
    repair_json_arguments,
    repair_note_for_result,
    _STREAM_REPAIR_SIZE_LIMIT,
)

logger = get_logger(__name__)

# 兼容导入：tool_call_loop 等模块仍从本模块导入这两个常量；
# 定义在 json_repair.py（单一数据源）。

_TEXTUAL_TOOL_CALL_RE = re.compile(
    r"<(?:longcat_)?tool_call\b[^>]*>.*?(?:</(?:longcat_)?tool_call\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)


def _get_tool_description_from_args(fn_args: dict) -> Optional[str]:
    """从工具参数中获取简短描述（优先使用 description，其次 _summary）"""
    if not fn_args:
        return None
    desc = fn_args.get("description") or fn_args.get("_summary")
    if desc and isinstance(desc, str):
        desc = desc.strip()
        if len(desc) > 80:
            desc = desc[:80] + "..."
        return desc
    return None


# ---------- text_editor 工具块摘要（文件名 + 行数差异） ----------
def _editor_target_name(fn_args: dict) -> str:
    """text_editor 目标文件名（带后缀）：取 path 的最后一段。

    无有效文件名（path 缺失、为 '.' 或 '/' 等根引用）返回空串，
    调用方据此退回不含文件名的旧文案。
    """
    path = str((fn_args or {}).get("path") or "").strip().replace("\\", "/")
    if not path:
        return ""
    trimmed = path.rstrip("/")
    if trimmed in ("", ".", "/"):
        return ""
    return trimmed.split("/")[-1]


def _count_edit_lines(text: Any) -> int:
    """按显示行数统计文本行数（尾部换行不计作新行；非字符串按 0 行）。"""
    if not isinstance(text, str) or not text:
        return 0
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return len(lines)


def _editor_diff_suffix(fn_args: dict, command: str) -> str:
    """根据参数计算 text_editor 写操作的 ``+n -n`` 行数差异后缀。

    - create：+file_text 行数（新建无删除）；
    - str_replace：-old_str 行数、+new_str 行数；
    - insert：+插入文本（insert_text 或 new_str）行数；
    - view / 未知命令：无后缀。

    展示规则：双方都大于 0 显示 `` +a -r``；只有一方大于 0 时只显示
    对应一侧（`` +a`` 或 `` -r``）；均为 0 则不加后缀。数值直接来自
    参数（流式期间随参数增量增长，实现工具块动态刷新）。
    """
    if command == "create":
        added, removed = _count_edit_lines((fn_args or {}).get("file_text")), 0
    elif command == "str_replace":
        removed = _count_edit_lines((fn_args or {}).get("old_str"))
        added = _count_edit_lines((fn_args or {}).get("new_str"))
    elif command == "insert":
        text = (fn_args or {}).get("insert_text")
        if text is None:
            text = (fn_args or {}).get("new_str")
        added, removed = _count_edit_lines(text), 0
    else:
        return ""
    if added > 0 and removed > 0:
        return f" +{added} -{removed}"
    if added > 0:
        return f" +{added}"
    if removed > 0:
        return f" -{removed}"
    return ""


def _coerce_positive_int(value: Any, default: int = 1) -> int:
    try:
        num = int(value)
        return num if num > 0 else default
    except (TypeError, ValueError):
        return default


def _requested_action(fn_args: dict) -> str:
    """todo / memory 等动作型工具的请求动作（fn_args.action，缺省 list）。"""
    return str((fn_args or {}).get("action") or "list").strip().lower()


def _coord_label(value: Any) -> str:
    """坐标对压缩为 4 位小数以内（折叠块标题展示用，不改执行参数）。"""
    text = " ".join(str(value or "").split())
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 2:
        return text
    try:
        lng = float(parts[0])
        lat = float(parts[1])
    except (TypeError, ValueError):
        return text
    def _fmt(v: float) -> str:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{_fmt(lng)},{_fmt(lat)}"


def _route_endpoint_label(fn_args: dict, *keys: str) -> str:
    """路线类工具的起讫点展示标签：多个候选键取第一个非空值。"""
    for key in keys:
        raw = (fn_args or {}).get(key)
        if raw is None or not str(raw).strip():
            continue
        text = str(raw).strip()
        if ";" in text:
            text = text.split(";", 1)[0].strip()
        return _coord_label(text)
    return ""


def _short_label(text: Any, limit: int = 24) -> str:
    """单行截短的对象名（todo 标题 / memory 内容摘要）：压缩空白后按字符截断。"""
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    return s[:limit] + "…" if len(s) > limit else s




def _todo_summary_done(fn_args: dict, payload: dict) -> str:
    """todo 完成态摘要：「动作 + 待办标题」，无标题退化为基础文案。

    done/undone 在执行器里都走 _op_toggle（结果 action 统一为 toggle），
    实际结果方向以 payload.todo.done 为准；payload 缺失时按请求意图兜底。
    """
    action = _requested_action(fn_args)
    todo_raw = payload.get("todo")
    todo: dict = todo_raw if isinstance(todo_raw, dict) else {}
    label = _short_label(todo.get("title"))
    obj = f" todo {label}" if label else " a todo"
    if action == "add":
        return f"Added{obj}"
    if action == "done":
        return f"Completed{obj}"
    if action == "undone":
        return f"Reopened{obj}"
    if action == "toggle":
        done_flag = todo.get("done")
        if done_flag is True:
            return f"Completed{obj}"
        if done_flag is False:
            return f"Reopened{obj}"
        return f"Updated{obj}"
    if action == "edit":
        return f"Updated{obj}"
    if action == "delete":
        return f"Deleted{obj}"
    if action == "clear":
        try:
            removed = int(payload.get("removed"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            removed = 0
        return f"Cleared {removed} todos" if removed > 0 else "Cleared the todo list"
    return "Listed todos"


def _memory_summary_done(fn_args: dict, payload: dict) -> str:
    """memory 完成态摘要：「动作 + 记忆内容摘要」，无内容退化为基础文案。"""
    action = _requested_action(fn_args)
    mem_raw = payload.get("memory")
    mem: dict = mem_raw if isinstance(mem_raw, dict) else {}
    label = _short_label(mem.get("content"))
    obj = f" memory: {label}" if label else " a memory"
    if action == "add":
        return f"Saved{obj}"
    if action == "get":
        return f"Retrieved{obj}"
    if action == "update":
        return f"Updated{obj}"
    if action == "delete":
        return f"Deleted{obj}"
    if action == "clear":
        try:
            removed = int(payload.get("removed"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            removed = 0
        return f"Cleared {removed} memories" if removed > 0 else "Cleared memories"
    if action == "search":
        return "Searched memories"
    return "Listed memories"



def _map_payload_from_result(result_content: Any) -> dict:
    """尽力把工具结果解析成 dict（todo / memory / subagent / 地图的结果
    都是 JSON 信封）；失败时返回空 dict。dict 输入原样直通。"""
    if isinstance(result_content, dict):
        return result_content
    try:
        payload = json.loads(str(result_content or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _short_summary_text(value: Any, limit: int = 54) -> str:
    """压缩空白并截断工具折叠块标题，避免 Telegram summary 过长。"""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _format_map_coordinate(value: Any) -> str:
    """把经纬度统一为紧凑可读形式；不改变原始执行参数。"""
    text = " ".join(str(value or "").split())
    parts = [item.strip() for item in text.split(",")]
    if len(parts) != 2:
        return _short_summary_text(value, 30)
    try:
        lng = float(parts[0])
        lat = float(parts[1])
    except (TypeError, ValueError):
        return _short_summary_text(value, 30)
    return f"{lng:.6f},{lat:.6f}"


def _format_map_radius(value: Any) -> str:
    try:
        meters = int(float(str(value)))
    except (TypeError, ValueError):
        return _short_summary_text(value, 20) or "default"
    if meters >= 1000:
        km = meters / 1000
        return f"{km:.1f} km" if km < 10 else f"{km:g} km"
    return f"{meters} m"


def _map_result_count(payload: dict) -> Optional[int]:
    """从常见地图返回结构提取结果数量（含 gaode_mcp 原生形状）。"""
    for key in ("count", "result_count", "total", "pois_count"):
        value = payload.get(key)
        try:
            if value is not None and int(value) >= 0:
                return int(value)
        except (TypeError, ValueError):
            pass
    for key in ("pois", "results", "geocodes", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            nested = _map_result_count(value)
            if nested is not None:
                return nested
    return None


def _map_location_query_label(fn_name: str, fn_args: dict) -> str:
    """提取地理编码工具本次真正查询的对象：地址或坐标。"""
    fn_args = fn_args or {}
    address = str(fn_args.get("address") or "").strip()
    if address:
        return _short_summary_text(address, 72)

    # 为兼容反向地理编码/未来扩展，坐标优先从常见字段中读取。
    location = fn_args.get("location") or fn_args.get("coordinates") or fn_args.get("coordinate")
    if location:
        formatted = _format_map_coordinate(location)
        if formatted:
            return formatted

    lng = fn_args.get("longitude", fn_args.get("lng"))
    lat = fn_args.get("latitude", fn_args.get("lat"))
    if lng is not None and lat is not None:
        return _format_map_coordinate(f"{lng},{lat}")
    return ""


# MCP 工具名规范化
def _norm_tool_key(fn_name: str) -> str:
    """完整 MCP 名只去掉 mcp__server__ 前缀，不映射旧工具别名。"""
    split = split_mcp_name(fn_name or "")
    return split[1] if split else (fn_name or "")


def _initial_map_tool_summary(fn_name: str, fn_args: dict) -> str | None:
    """地图工具进行态：把真正的查询条件放进折叠块标题。"""
    fn_args = fn_args or {}
    if fn_name in {"maps_geo", "maps_regeocode"}:
        query = _map_location_query_label(fn_name, fn_args)
        if fn_name == "maps_regeocode":
            return f"Reverse geocoding {query}" if query else "Reverse geocoding"
        return f"Geocoding {query}" if query else "Geocoding"
    if fn_name == "maps_text_search":
        keywords = _short_summary_text(fn_args.get("keywords"), 42)
        city = _short_summary_text(fn_args.get("city"), 24)
        scope = f" · {city}" if city else ""
        return f"Searching POIs: {keywords}{scope}" if keywords else "Searching POI by keyword"
    if fn_name == "maps_around_search":
        keywords = _short_summary_text(fn_args.get("keywords"), 34)
        location = _format_map_coordinate(fn_args.get("location")) if fn_args.get("location") else ""
        radius = _format_map_radius(fn_args.get("radius")) if fn_args.get("radius") is not None else "1 km"
        parts = [part for part in (keywords, f"center {location}" if location else "", f"radius {radius}") if part]
        return "Searching nearby: " + " · ".join(parts) if parts else "Searching nearby POI"
    return None


def _done_map_tool_summary(fn_name: str, fn_args: dict, result_content: Any) -> str | None:
    """地图工具完成态：显示查询对象 + 查询范围 + 结果量/解析结果。"""
    fn_args = fn_args or {}
    payload = _map_payload_from_result(result_content)
    if fn_name in {"maps_geo", "maps_regeocode"}:
        query = _map_location_query_label(fn_name, fn_args)
        geocodes = payload.get("geocodes")
        count = len(geocodes) if isinstance(geocodes, list) else _map_result_count(payload)
        if count is not None:
            noun = "match" if count == 1 else "matches"
            action = "Reverse geocoded" if fn_name == "maps_regeocode" else "Geocoded"
            return f"{action} {query} · {count} {noun}" if query else f"{action} · {count} {noun}"
        action = "Reverse geocoded" if fn_name == "maps_regeocode" else "Geocoded"
        return f"{action} {query}" if query else action

    if fn_name == "maps_text_search":
        keywords = _short_summary_text(fn_args.get("keywords"), 42)
        city = _short_summary_text(fn_args.get("city"), 24)
        count = _map_result_count(payload)
        parts = [part for part in (keywords, city) if part]
        if count is not None:
            parts.append(f"{count} results")
        return _short_summary_text("Searched POIs: " + " · ".join(parts), 140) if parts else "Searched POIs by keyword"

    if fn_name == "maps_around_search":
        keywords = _short_summary_text(fn_args.get("keywords"), 34)
        location = _format_map_coordinate(fn_args.get("location")) if fn_args.get("location") else ""
        radius = _format_map_radius(fn_args.get("radius")) if fn_args.get("radius") is not None else "1 km"
        count = _map_result_count(payload)
        parts = [part for part in (keywords, f"center {location}" if location else "", f"radius {radius}") if part]
        if count is not None:
            parts.append(f"{count} results")
        return _short_summary_text("Nearby POIs: " + " · ".join(parts), 150) if parts else "Searched nearby POIs"
    return None

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
    优先使用自定义 description，否则按照规范显示固定进行时文本。
    """
    fn_args = fn_args or {}
    fn_name = _norm_tool_key(fn_name)

    # web_search 单工具进行态固定显示搜索词。str() 防御：类型错误的
    # query（如数字）会被 L2 校验拦截回传，但 UI 摘要必须先不崩。
    if fn_name == "web_search":
        query = str(fn_args.get("query") or "").strip()
        return query if query else "Searching the web"

    # ---------- text_editor ----------
    # 注意：text_editor 不再声明 description（意图）参数，摘要一律按
    # 「动作 + 文件名 + 行数差异」规范生成，模型即使惯性带上 description
    # 也不被采用（因此本分支必须位于 custom_desc 检查之前）。
    if fn_name == "text_editor":
        command = str(fn_args.get("command") or "")
        name = _editor_target_name(fn_args)
        suffix = _editor_diff_suffix(fn_args, command)
        if command == "view":
            return f"Viewing file {name}" if name else "Viewing file"
        if command == "create":
            return f"Creating file {name}{suffix}" if name else "Creating file"
        if command in ("str_replace", "insert"):
            return f"Editing file {name}{suffix}" if name else "Editing file"
        return f"Editing file {name}" if name else "Editing file"

    # ---------- todo / memory / subagent / deliver_reply ----------
    # 与 text_editor 同规范：这些工具不声明 description（意图）参数，
    # 进行态摘要一律按「动作 + 对象」规范生成，模型即使惯性带上
    # description 也不被采用（因此本分支必须位于 custom_desc 检查之前）。
    if fn_name == "todo":
        action = _requested_action(fn_args)
        if action == "add":
            return "Adding a todo"
        if action == "done":
            return "Completing a todo"
        if action == "undone":
            return "Reopening a todo"
        if action in ("toggle", "edit"):
            return "Updating a todo"
        if action == "delete":
            return "Deleting a todo"
        if action == "clear":
            return "Clearing the todo list"
        return "Listing todos"

    if fn_name == "memory":
        action = _requested_action(fn_args)
        if action == "add":
            return "Saving a memory"
        if action == "search":
            return "Searching memories"
        if action == "get":
            return "Retrieving a memory"
        if action == "update":
            return "Updating a memory"
        if action == "delete":
            return "Deleting a memory"
        if action == "clear":
            return "Clearing memories"
        return "Listing memories"

    if fn_name == "subagent":
        # 进行态标题直接展示子任务内容（对标 Claude Code 的 Task 卡片）。
        task = _short_summary_text(fn_args.get("task"), 48)
        if task:
            return f"Running a subagent: {task}"
        return "Running a subagent"

    if fn_name == "deliver_reply":
        return "Delivering the final reply"

    custom_desc = _get_tool_description_from_args(fn_args)
    if custom_desc:
        return custom_desc

    # ---------- 特殊处理 ----------

    if fn_name == "fetch_url":
        url = str(fn_args.get("url") or "").strip()
        domain = extract_domain(url) if url else ""
        return f"Fetching from {domain}" if domain else "Fetching a page"

    if fn_name == "bash":
        # 后台任务模式的专属进行态（此前与前台命令共用 "Running command"）。
        task_action = str(fn_args.get("task_action") or "").strip().lower()
        if task_action in ("status", "output"):
            return "Checking background task"
        if task_action == "list":
            return "Listing background tasks"
        if task_action == "stop":
            return "Stopping background task"
        if fn_args.get("run_in_background"):
            cmd = str(fn_args.get("command") or "").strip()
            if cmd:
                short_cmd = cmd[:30] + "..." if len(cmd) > 30 else cmd
                return f"Starting background: {short_cmd}"
            return "Starting background command"
        cmd = str(fn_args.get("command") or "").strip()
        if cmd:
            short_cmd = cmd[:30] + "..." if len(cmd) > 30 else cmd
            return short_cmd
        return "Running command"

    # ---------- 图片类 ----------
    # 统一图像工具 generate_image：按 image_url 是否携带判断生成/编辑，
    # 折叠块标题显示对应操作。
    if fn_name == "generate_image":
        is_edit = bool(str(fn_args.get("image_url") or "").strip())
        if is_edit:
            return "Editing an image"
        num_images = _coerce_positive_int(fn_args.get("num_images"), 1)
        if num_images == 1:
            return "Generating an image"
        return f"Generating {num_images} images"

    if fn_name == "generate_video":
        prompt = _short_summary_text(fn_args.get("prompt"), 36)
        if prompt:
            return f"Generating a video: {prompt}"
        return "Generating a video"

    if fn_name == "present_files":
        paths = fn_args.get("paths")
        names = []
        if isinstance(paths, list):
            for p in paths:
                if isinstance(p, str) and p.strip():
                    names.append(p.strip().rstrip("/").split("/")[-1])
        n = len(names)
        if n == 0:
            return "Presenting file(s)"
        shown = "、".join(names[:2]) + (" 等" if n > 2 else "")
        label = "file" if n == 1 else "files"
        return f"Presenting {n} {label}: {shown}"

    if fn_name == "message_user":
        return "Waiting for your answer"

    # ---------- 地图工具：查询条件必须直接进入折叠块标题 ----------
    map_summary = _initial_map_tool_summary(fn_name, fn_args)
    if map_summary:
        return map_summary

    if fn_name == "maps_ip_location":
        return "Locating IP origin"

    if fn_name in {"maps_direction_driving", "maps_direction_walking",
                   "maps_direction_bicycling", "maps_direction_transit_integrated"}:
        origin = _route_endpoint_label(fn_args, "origin", "origin_point", "from")
        destination = _route_endpoint_label(fn_args, "destination", "dest", "to")
        mode = {
            "maps_direction_driving": "driving",
            "maps_direction_walking": "walking",
            "maps_direction_bicycling": "cycling",
            "maps_direction_transit_integrated": "transit",
        }[fn_name]
        if origin and destination:
            return f"Planning {mode} route: {origin} → {destination}"
        return f"Planning {mode} route"

    if fn_name == "maps_distance":
        origin = _route_endpoint_label(fn_args, "origins", "origin")
        destination = _route_endpoint_label(fn_args, "destinations", "destination", "dest")
        if origin and destination:
            return f"Measuring distance: {origin} → {destination}"
        return "Measuring distance"

    # ---------- 其他工具，按规范进行时文本 ----------
    if fn_name == "weather":
        city = _short_summary_text(fn_args.get("city"), 24)
        return f"Fetching weather for {city}" if city else "Fetching weather"

    if fn_name == "exchange_rate":
        base = _short_summary_text(fn_args.get("base"), 8).upper() or None
        target = _short_summary_text(fn_args.get("target"), 8).upper() or None
        if base and target:
            return f"Checking exchange rate: {base} → {target}"
        if base:
            return f"Checking {base} exchange rates"
        return "Checking exchange rates"

    if fn_name == "wikipedia":
        query = _short_summary_text(fn_args.get("query"), 36)
        return f"Looking up {query} on Wikipedia" if query else "Looking up on Wikipedia"

    mapping = {
        "present_files": "Presenting file(s)",
        "maps_geo": "Geocoding address",
        "maps_search_detail": "Fetching POI details",
    }
    return mapping.get(fn_name, "Running...")


# text_editor 的 command 封闭枚举：工具名尚未到达时，可据参数形状把
# 占位条目的进行态摘要推断为 text_editor 风格（如 "Creating file"）。
# undo_edit 命令不存在，不属于合法枚举。
_TEXT_EDITOR_COMMAND_ENUM = frozenset({"view", "create", "str_replace", "insert"})


def _generate_pending_tool_summary(fn_args: dict) -> str:
    """工具名尚未到达时的进行态摘要（流式占位工具条目用）。

    部分网关的 tool_call 增量会先流式传输参数、后补发 id/函数名。
    在函数名到达前，尽量从参数形状推断一个有意义的进行态文本：
    text_editor 的 command 是封闭枚举，可直接识别（覆盖"创建文件"
    等最长参数流场景）；其余情况显示通用进行态，待函数名到达后由
    ``attach_stream_tool_identity`` 按真实工具名覆写。
    """
    command = str((fn_args or {}).get("command") or "")
    if command in _TEXT_EDITOR_COMMAND_ENUM:
        return _generate_initial_tool_summary("text_editor", fn_args or {})
    return "Preparing tool call..."


def _generate_action_description(fn_name: str, fn_args: Optional[dict] = None) -> str:
    """生成动作描述（用于 fallback）"""
    fn_args = fn_args or {}
    fn_name = _norm_tool_key(fn_name)

    if not fn_name:
        # 流式占位条目（函数名尚未到达）没有可描述的工具名：
        # 返回空串，让调用方落到各自的通用进行态文本。
        return ""

    # ---------- todo / memory / subagent / deliver_reply ----------
    # 与 text_editor 同规范：不声明 description，动作描述一律按
    # 「动作 + 对象」生成；本分支位于 custom_desc 检查之前，模型惯性
    # 携带的意图字段不被采用。工具组进行态标题由本描述首字母大写而来。
    if fn_name == "todo":
        action = _requested_action(fn_args)
        return {
            "add": "adding a todo",
            "done": "completing a todo",
            "undone": "reopening a todo",
            "toggle": "updating a todo",
            "edit": "updating a todo",
            "delete": "deleting a todo",
            "clear": "clearing the todo list",
        }.get(action, "listing todos")

    if fn_name == "memory":
        action = _requested_action(fn_args)
        return {
            "add": "saving a memory",
            "get": "retrieving a memory",
            "update": "updating a memory",
            "delete": "deleting a memory",
            "clear": "clearing memories",
            "search": "searching memories",
        }.get(action, "listing memories")

    if fn_name == "subagent":
        task = _short_summary_text(fn_args.get("task"), 40)
        return f"delegating to a subagent: {task}" if task else "delegating to a subagent"

    if fn_name == "deliver_reply":
        return "delivering the final reply"

    custom_desc = _get_tool_description_from_args(fn_args)
    if custom_desc:
        return custom_desc

    if fn_name == "text_editor":
        cmd = str(fn_args.get("command") or "")
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
        "weather": "fetched weather",
        "generate_video": "generated a video",
        "maps_geo": "geocoded an address",
        "maps_text_search": "searched for points of interest by keyword",
        "maps_around_search": "searched for nearby points of interest",
        "maps_search_detail": "fetched POI details",
        "maps_direction_driving": "planned a driving route",
        "maps_direction_walking": "planned a walking route",
        "maps_direction_bicycling": "planned a cycling route",
        "maps_direction_transit_integrated": "planned a transit route",
        "maps_distance": "measured a distance",
        "bash": "ran a command",
        "present_files": "presented files",
        "message_user": "messaged you",
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


# 有界快速字段扫描：只看头部前 4KB。command/path/query/url/description
# 等控制字段在参数对象的最前面（大体积负载如 file_text / new_str 排在其
# 后），因此即使参数超过修复器的尺寸闸门、或 JSON 尚未闭合，也能拿到
# 进行态摘要所需的关键字段。
_FAST_SCAN_PREFIX_LEN = 4096
_FAST_FIELD_RE = re.compile(
    r'"(command|path|query|url|description|_summary)"\s*:\s*"((?:[^"\\]|\\.)*)"'
)


def _fast_scan_fields(args_str: str) -> dict:
    """从（可能截断的）参数字符串头部快速提取控制字段（UI 摘要用途）。

    只匹配未转义的真实字段键——字符串值内部的引号在 JSON 里必然被
    转义（\\"），不会被误认成字段边界。同一字段取首次出现，反转义
    优先按 JSON 字符串字面量解析（与旧 description 正则相同的策略，
    可正确处理 \\uXXXX、\\\\ 等全部转义序列）。
    """
    fields: dict = {}
    head = (args_str or "")[:_FAST_SCAN_PREFIX_LEN]
    for match in _FAST_FIELD_RE.finditer(head):
        key = match.group(1)
        if key in fields:
            continue
        raw = match.group(2)
        try:
            fields[key] = json.loads(f'"{raw}"')
        except (json.JSONDecodeError, ValueError):
            # 兜底：极少数非法转义序列下退回到手工反转义。
            fields[key] = (
                raw.replace('\\"', '"')
                .replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace("\\\\", "\\")
            )
    return fields


def _safe_parse_args(args_str: str) -> dict:
    """尽力从参数字符串中提取可用的 dict（UI 摘要用途）。

    解析优先级：完整 json.loads → 保守自动修复（限流：仅小于
    ``_STREAM_REPAIR_SIZE_LIMIT`` 的输入；流式截断时允许猜测补全，
    因为结果仅用于展示预览，不会真正执行）→ 有界快速字段扫描。
    快速扫描不受尺寸闸门限制：超大参数（如 text_editor create 的整份
    ``file_text``）在流式期间也能把 "Creating file" 等进行态摘要及时
    上屏，而不是退化为泛化的 "Editing file"。
    """
    if not args_str:
        return {}
    try:
        parsed = json.loads(args_str)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    # 快速字段扫描结果：修复器可用时并入修复结果，否则单独作为兜底。
    fast_fields = _fast_scan_fields(args_str)
    # 保守自动修复（仅展示用途——允许补全截断，不进入执行层）。
    if len(args_str) < _STREAM_REPAIR_SIZE_LIMIT:
        try:
            repaired, _info = repair_json_arguments(
                args_str, allow_close_truncated=True)
            if isinstance(repaired, dict) and repaired:
                # 剔除修复提示键，只保留真实参数字段供摘要展示。
                repaired.pop(_JSON_REPAIR_NOTE_KEY, None)
                for key, value in fast_fields.items():
                    repaired.setdefault(key, value)
                return repaired
        except Exception:
            logger.debug("_safe_parse_args 修复兜底内部忽略的异常", exc_info=True)
            pass
    return fast_fields


def _normalize_tool_arguments(
        arguments: Any, stream_finish_reason: Optional[str] = None,
) -> tuple[str, bool, dict]:
    """规范化单个工具调用的参数，返回 ``(JSON 字符串, 是否写入可恢复错误, 元信息)``。

    v2.3 Self-Correction 增强后的处理链（优先级从高到低）：

    1. 原文即合法 JSON object → 重新序列化（去冗余空白），元信息
       ``{"kind": "valid"}``；
    2. 合法 JSON 但顶层非 object → 生成带 arg_kind 诊断的信封，执行层
       会告诉模型「参数是数组/字符串，必须是对象」；
    3. 畸形 JSON → 先尝试保守自动修复。修复成功且为 dict → 直接用修复
       后的参数（注入 ``__apitelegram_json_repair_note__`` 键，run_one
       会把它转成工具结果里的透明提示），元信息
       ``{"kind": "repaired", "fixes": [...]}``。工具照常执行，省掉
       一整轮模型重试；
    4. 修复失败（含截断——绝不猜测补全后执行）→ 生成带完整诊断的
       可恢复信封：解析器报错原文（行/列/字符位置）、出错位置上下文、
       病因清单、原始参数摘录。元信息 ``{"kind": "invalid", ...}``。

    信封/修复结果本身都是合法 JSON，保证回传 provider 不 400。

    v2.5：``stream_finish_reason``（可选）来自本轮流式/非流式响应的
    结束原因，透传给信封——finish_reason=length 时模型能明确知道
    「参数是被输出上限切断的」而非自己写坏了 JSON。
    """
    meta: dict = {"kind": "valid"}
    raw = arguments if isinstance(arguments, str) else str(arguments or "")
    # 注意：Python 3 的 `except ... as exc` 在块结束时删除绑定名，函数后段
    # 还要用解析器报错构建诊断信封，因此必须先把异常转移到不会被删除的
    # 局部变量里（旧写法直接引用 exc 会 UnboundLocalError——畸形且不可
    # 修复的参数恰恰是本函数最关键的路径）。
    parse_exc: Optional[Exception] = None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            # 重新序列化同时移除无意义空白，确保所有兼容端收到相同的合法 JSON。
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), False, meta
        # 合法 JSON 但顶层不是 object：不修复、直接诊断（数组/字符串等
        # 无法「修复」成对象，语义重排只能交给模型）。
        arg_kind = _kind_of_value(parsed)
        envelope = build_invalid_arguments_envelope(raw, arg_kind=arg_kind)
        meta = {"kind": "invalid", "reason": envelope.get(_INVALID_TOOL_ARGUMENTS_KEY)}
        return (
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            True,
            meta,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        parse_exc = exc

    # 畸形 JSON：先尝试保守自动修复（截断不猜测补全，安全优先）。
    repaired, repair_info = repair_json_arguments(raw, allow_close_truncated=False)
    if isinstance(repaired, dict):
        # repair_info 形状由 repair_json_arguments 保证："fixes" 恒为 list。
        note = repair_note_for_result(cast(list, repair_info.get("fixes")))
        if note:
            repaired[_JSON_REPAIR_NOTE_KEY] = note
        meta = {"kind": "repaired", "fixes": repair_info.get("fixes", [])}
        return (
            json.dumps(repaired, ensure_ascii=False, separators=(",", ":")),
            False,
            meta,
        )

    # 修复失败：生成带完整诊断的可恢复信封（parse_exc 携带解析器原始
    # 报错——行/列/字符位置——由信封透传给模型做 Self-Correction）。
    envelope = build_invalid_arguments_envelope(
        raw, exc=parse_exc, stream_finish_reason=stream_finish_reason)
    meta = {
        "kind": "invalid",
        "reason": envelope.get(_INVALID_TOOL_ARGUMENTS_KEY),
        "parse_error": envelope.get("parse_error"),
        "truncated": envelope.get("looks_truncated", False),
        "stream_cut": envelope.get("stream_cut", False),
    }
    return (
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
        True,
        meta,
    )


def _kind_of_value(parsed: Any) -> str:
    """合法 JSON 非对象值的可读描述（用于 arg_kind 诊断）。"""
    if isinstance(parsed, list):
        return "a JSON array"
    if isinstance(parsed, str):
        return "a JSON string"
    if isinstance(parsed, bool):
        return "a JSON boolean"
    if isinstance(parsed, (int, float)):
        return "a JSON number"
    if parsed is None:
        return "JSON null"
    return type(parsed).__name__


def _normalize_tool_call_arguments(
        tool_calls: list[dict], api_label: str, round_number: int,
        stream_finish_reason: Optional[str] = None,
) -> int:
    """就地规范化一个模型返回中的所有工具参数，并返回写入可恢复错误的数量。

    v2.3：自动修复与不可修复分别记日志——修复意味着零重试成本直接恢复，
    不可修复才走可恢复错误路径。两者都不再把坏字符串写回下一轮请求。
    v2.5：``stream_finish_reason``（可选）为空参数/截断参数的根因定性和
    指引方向提供决定性证据（length = 输出上限切断；"" = 断流；
    stop/tool_calls = 正常结束、语法问题在模型自身）。
    """
    corrected = 0
    repaired_count = 0
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        function = tc.setdefault("function", {})
        if not isinstance(function, dict):
            tc["function"] = function = {}
        normalized, was_corrected, meta = _normalize_tool_arguments(
            function.get("arguments", ""), stream_finish_reason=stream_finish_reason)
        function["arguments"] = normalized
        if was_corrected:
            corrected += 1
        elif meta.get("kind") == "repaired":
            repaired_count += 1
    if repaired_count:
        logger.info(
            "[%s] 第 %s 轮自动修复了 %s 个畸形工具参数 JSON（工具将直接用修复后的参数执行，无需模型重试）",
            api_label, round_number, repaired_count,
        )
    if corrected:
        logger.warning(
            "[%s] 第 %s 轮检测到 %s 个无法自动修复的工具参数 JSON，已写入带诊断的可恢复错误并阻止其污染下一轮请求"
            "（流结束原因 finish_reason=%r，stream_cut=%s）",
            api_label, round_number, corrected, stream_finish_reason,
            bool(_finish_reason_cut_info(stream_finish_reason)[0]),
        )
    return corrected


def _tool_result_is_failure(fn_name: str, fn_args: dict, result_content: Any, details_html: str = "") -> bool:
    """统一判断工具是否失败；失败项不会进入工具组成功统计。"""
    fn_name = _norm_tool_key(fn_name)
    if result_content == _TOOL_TIMEOUT_MARKER:
        return True
    text = str(result_content or "").strip()
    lower = text.lower()
    if fn_name == "bash":
        # Bash 工具是否失败只取决于工具执行本身；命令返回非零只是命令输出，
        # 不应把工具条目标成 error。明确的工具级错误仍由这些前缀识别。
        return lower.startswith(("error:", "exception:", "failed:", "timeout:", "❌"))
    if lower.startswith(("error:", "exception:", "failed:", "timeout:", "❌", "失败：", "失败:")):
        return True
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and payload.get("error"):
                return True
        except Exception:
            logger.debug("_tool_result_is_failure 内部忽略的异常", exc_info=True)
            pass
    return False


def _distance_label(value: Any) -> str:
    """米 → 折叠块标题用的紧凑距离（English 风格）。"""
    try:
        meters = float(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    if meters >= 1000:
        return f"{meters / 1000:.1f} km"
    return f"{meters:g} m"


def _duration_label(value: Any) -> str:
    """秒 → 折叠块标题用的紧凑时长。"""
    try:
        seconds = float(str(value or "").strip())
    except (TypeError, ValueError):
        return ""
    minutes = seconds / 60
    if minutes >= 60:
        return f"{int(minutes // 60)} h {int(round(minutes % 60))} min"
    return f"{minutes:.0f} min" if minutes >= 1 else f"{seconds:g} s"


def _route_done_summary(fn_name: str, result_content: Any) -> str | None:
    """路线/距离工具完成态：补充距离与时长（若无结果载荷返回 None 退化）。"""
    try:
        payload = json.loads(str(result_content or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    route = payload.get("route")
    if fn_name == "maps_distance":
        results = payload.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            distance = _distance_label(results[0].get("distance"))
            duration = _duration_label(results[0].get("duration"))
            bits = [b for b in (distance, duration) if b]
            if bits:
                return f"Measured a distance · {' · '.join(bits)}"
        return None
    if not isinstance(route, dict):
        return None
    mode = {
        "maps_direction_driving": "driving",
        "maps_direction_walking": "walking",
        "maps_direction_bicycling": "cycling",
        "maps_direction_transit_integrated": "transit",
    }.get(fn_name)
    if not mode:
        return None
    distance = ""
    duration = ""
    paths = route.get("paths")
    transits = route.get("transits")
    if isinstance(paths, list) and paths and isinstance(paths[0], dict):
        distance = _distance_label(paths[0].get("distance"))
        duration = _duration_label(paths[0].get("duration"))
    elif isinstance(transits, list) and transits and isinstance(transits[0], dict):
        duration = _duration_label(transits[0].get("duration"))
    bits = [b for b in (distance, duration) if b]
    suffix = f" · {' · '.join(bits)}" if bits else ""
    return f"Planned a {mode} route{suffix}"


_IMAGE_TOOL_NAMES = frozenset({"generate_image"})


def image_result_count(fn_name: str, result_content: str) -> int | None:
    """从图片工具的成功结果里数出实际返回的图片张数（按链接行计）。

    请求张数（num_images）只是意图：供应商可能少给（ModelScope 并发子任务部分失败、
    R2 部分上传失败），编辑任务也固定只出一张。展示文案必须按真实产出，
    否则折叠块标题与卡片里的图片数量对不上。非图片工具 / 失败结果返回 None。
    """
    if _norm_tool_key(fn_name) not in _IMAGE_TOOL_NAMES:
        return None
    text = str(result_content or "")
    if "✅" not in text:
        return None
    n = sum(1 for line in text.splitlines() if line.strip().startswith(("http://", "https://")))
    return n or None


def _generate_tool_summary_done(fn_name: str, fn_args: dict, result_content: str) -> str:
    """生成当前工具完成后的用户可见摘要。"""
    fn_args = fn_args or {}
    fn_name = _norm_tool_key(fn_name)

    if fn_name == "web_search":
        query = str(fn_args.get("query") or "").strip()
        count = _extract_web_search_result_count(result_content)
        if query and count is not None:
            return f"{query} {count} result" if count == 1 else f"{query} {count} results"
        return "Searched the web"

    # ---------- text_editor ----------
    # text_editor 不再声明 description（意图）参数：完成态摘要一律按
    # 「动作 + 文件名 + 行数差异」规范生成（本分支位于 custom_desc 检查
    # 之前，模型惯性携带的 description 不会被采用）。
    if fn_name == "text_editor":
        command = str(fn_args.get("command") or "")
        name = _editor_target_name(fn_args)
        suffix = _editor_diff_suffix(fn_args, command)
        if command == "view":
            return f"Viewed file {name}" if name else "Viewed a file"
        if command == "create":
            return f"Created file {name}{suffix}" if name else "Created a file"
        if command in ("str_replace", "insert"):
            return f"Edited file {name}{suffix}" if name else f"Edited a file{suffix}"
        return f"Edited file {name}" if name else "Edited a file"

    # ---------- todo / memory / subagent / deliver_reply ----------
    # 与 text_editor 同规范：完成态摘要按「动作 + 对象」生成，不再退化为
    # 笼统的 "Ran an action"（本分支位于 custom_desc 检查之前）。
    if fn_name == "todo":
        return _todo_summary_done(fn_args, _map_payload_from_result(result_content))

    if fn_name == "memory":
        return _memory_summary_done(fn_args, _map_payload_from_result(result_content))

    if fn_name == "subagent":
        # 完成态带上轮次/工具调用/耗时（对标 Claude Code 的统计尾注）；
        # 模型名/任务回声等细节仍只在展开卡片里展示。
        try:
            payload = json.loads(str(result_content or ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            if not payload.get("ok"):
                return "Ran a subagent (failed)"
            bits = []
            if payload.get("rounds"):
                bits.append(f"{payload.get('rounds')} rounds")
            if payload.get("tool_calls"):
                bits.append(f"{payload.get('tool_calls')} tool calls")
            elapsed = payload.get("elapsed")
            if isinstance(elapsed, (int, float)) and elapsed > 0:
                bits.append(f"{elapsed:.0f}s")
            if bits:
                return "Ran a subagent (" + " · ".join(bits) + ")"
        return "Ran a subagent"

    if fn_name == "deliver_reply":
        # send=false（或 TIMER 回合缺省 false）时结果是「未发送：…」；
        # 真正失败（"失败："前缀）走 error 路径，不会进入本函数。
        if str(result_content or "").startswith("未发送"):
            return "Skipped the final reply"
        return "Delivered the final reply"

    map_summary = _done_map_tool_summary(fn_name, fn_args, result_content)
    if map_summary:
        return map_summary

    if fn_name == "maps_ip_location":
        return "Located IP origin"

    if fn_name in {"maps_direction_driving", "maps_direction_walking",
                   "maps_direction_bicycling", "maps_direction_transit_integrated", "maps_distance"}:
        route_summary = _route_done_summary(fn_name, result_content)
        if route_summary:
            return route_summary
        return {
            "maps_direction_driving": "Planned a driving route",
            "maps_direction_walking": "Planned a walking route",
            "maps_direction_bicycling": "Planned a cycling route",
            "maps_direction_transit_integrated": "Planned a transit route",
            "maps_distance": "Measured a distance",
        }[fn_name]

    if fn_name == "maps_search_detail":
        try:
            payload = json.loads(str(result_content or ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict) and _short_summary_text(payload.get("name"), 40):
            return f"Fetched POI details: {_short_summary_text(payload.get('name'), 40)}"

    custom_desc = _get_tool_description_from_args(fn_args)
    if custom_desc:
        return custom_desc

    if fn_name == "fetch_url":
        url = str(fn_args.get("url") or "").strip()
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
            m = re.search(r"<title>(.*?)</title>", text, re.I | re.S)
            if m:
                title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                title = re.sub(r"\s+", " ", title)
        return f"Fetched: {title}" if title else (f"Fetched: {domain}" if domain else "Fetched a page")

    if fn_name == "wikipedia":
        query = str(fn_args.get("query") or "").strip()
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

    if fn_name == "message_user":
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
                return "User is away (no reply)"
        except Exception:
            logger.debug("_generate_tool_summary_done 内部忽略的异常", exc_info=True)
            pass
        return "User answered"

    if fn_name == "bash":
        task_action = str(fn_args.get("task_action") or "").strip().lower()
        if task_action in ("status", "output"):
            return "Checked a background task"
        if task_action == "list":
            return "Listed background tasks"
        if task_action == "stop":
            return "Stopped a background task"
        if fn_args.get("run_in_background"):
            return "Started a background command"
        return "Ran a command"

    if fn_name == "present_files":
        # 完成态从结果信封读取真实成败（此前只看请求参数里的路径数）。
        try:
            payload = json.loads(str(result_content or ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        sent = payload.get("sent") if isinstance(payload, dict) else None
        failed = payload.get("failed") if isinstance(payload, dict) else None
        sent_n = len(sent) if isinstance(sent, list) else None
        failed_n = len(failed) if isinstance(failed, list) else None
        if sent_n is None and failed_n is None:
            paths = fn_args.get("paths", [])
            n = len(paths) if isinstance(paths, list) else 0
            return "Presented file" if n <= 1 else f"Presented {n} files"
        parts = []
        if sent_n and isinstance(sent, list):
            names = [str(x) for x in sent[:2]]
            listing = ", ".join(names) + ("…" if sent_n > 2 else "")
            label = "file" if sent_n == 1 else "files"
            parts.append(f"Sent {sent_n} {label} ({listing})")
        if failed_n:
            parts.append(f"{failed_n} failed")
        return ", ".join(parts) if parts else "Presented files"

    # 图片完成态按结果里真实的图片张数（image_result_count）；解析不到（失败文案 /
    # 非标准结果）才退回请求张数，保证标题与下方卡片里的图片数一致。
    # 统一图像工具：按 image_url 是否携带区分生成/编辑完成态文案。
    if fn_name == "generate_image":
        is_edit = bool(str(fn_args.get("image_url") or "").strip())
        if is_edit:
            n = image_result_count(fn_name, result_content) or 1
            return "Edited an image" if n == 1 else f"Edited {n} images"
        n = image_result_count(fn_name, result_content) or _coerce_positive_int(fn_args.get("num_images"), 1)
        return "Generated an image" if n == 1 else f"Generated {n} images"
    if fn_name == "generate_video":
        return "Generated a video"

    if fn_name == "weather":
        # 完成态直接携带结果要点（对标 POI/页面标题式完成摘要）：
        # 「Fetched weather: 北京 25°C 多云」。失败退化为基础文案。
        try:
            payload = json.loads(str(result_content or ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict) and not payload.get("error"):
            city = _short_summary_text(payload.get("city"), 24)
            temp = str(payload.get("current", {}).get("temp", "") or "").strip()
            unit = "°F" if str(payload.get("unit", "")).upper() == "F" else "°C"
            cond = _short_summary_text(payload.get("current", {}).get("condition", ""), 20)
            if city and temp:
                cond_txt = f" {cond}" if cond else ""
                return f"Fetched weather: {city} {temp}{unit}{cond_txt}"
        return "Fetched weather"

    if fn_name == "exchange_rate":
        # 结果是 HTML 文本「1 USD = 7.2400 CNY」：提取汇率写进标题。
        base = _short_summary_text(fn_args.get("base"), 8).upper()
        target = _short_summary_text(fn_args.get("target"), 8).upper()
        text = str(result_content or "")
        if base and target:
            m = re.search(rf"1\s+{re.escape(base)}\s*=\s*([0-9.]+)\s+{re.escape(target)}", text)
            if m:
                return f"Checked exchange rate {base} → {target}: {m.group(1)}"
            return f"Checked exchange rate {base} → {target}"
        if base:
            return f"Checked {base} exchange rates"
        return "Checked exchange rates"

    mapping = {
        "wikipedia": "Looked up on Wikipedia",
        "maps_geo": "Geocoded",
        "nearby_search": "Searched nearby",
        "maps_text_search": "Searched POIs by keyword",
        "maps_around_search": "Searched nearby POIs",
        "public_holidays": "Looked up holidays",
        "convert": "Calculated a result",
    }
    return mapping.get(fn_name, "Ran an action")


