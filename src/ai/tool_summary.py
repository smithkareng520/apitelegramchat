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
from ai._constants import MAX_TOOL_CALLS
from ai.error_formatting import extract_domain
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

# =====================================================================
# bash 复合命令"部分成功"识别
# =====================================================================
# 场景：`curl -o x.mp3 URL && ls -lh x.mp3 && file x.mp3` —— 容器里没装
# `file` 时整链退出码为 127，但主体工作（下载）已实际完成。旧判定
# "退出码非 0 即失败" 会把这种命令标成 error，工具组摘要退化为
# "Tools failed"，与用户看到的事实（文件已下载）直接矛盾。
#
# 判定依据（三条全部满足才判部分成功）：
#   1. 命令含顺序分隔符（&& / ; / 换行），是复合命令；
#   2. 输出中存在 shell 的 "command not found" 诊断行；
#   3. 缺失的命令不是命令链的首命令（首命令 = 主体工作）。
# 对于 && 链这是可证明的：短路语义保证后段命令能被执行，当且仅当
# 前面所有段都已成功。
#
# 已知局限（可接受）：`for x in …; do missing_cmd …; done` 这类首 token
# 是 shell 关键字的命令可能被误判为部分成功。误判代价很低——完整输出
# （含错误文本与退出码）仍会回传给模型自行补救，UI 只是少标一个红叉；
# 而漏判的代价正是本 bug：用户看到 "Tools failed" 但实际工作已完成。

# bash/dash/zsh 的 "command not found" 诊断行，捕获缺失的命令名：
#   /bin/bash: line 196: file: command not found   → file
#   bash: file: command not found                   → file
#   sh: 1: file: not found                          → file (dash)
_BASH_CMD_NOT_FOUND_RE = re.compile(
    r"(?im)(?:^|\s)(?:/[\w./-]+)?(?:ba|z|da)?sh"
    r"(?::\s*(?:(?:line\s+)?\d+:)?)?\s+([^\s:;]+)\s*:\s+(?:command\s+)?not found\b"
)
# zsh 格式：command not found: file
_ZSH_CMD_NOT_FOUND_RE = re.compile(r'(?im)command not found:\s*([^\s:;"]+)')

# 顺序分隔符：&& / 单个 & / ; / 换行。刻意不匹配 | 和 || ——
# 管道是单条流水线，尾端命令缺失时（如 `curl … | file -`）流水线的
# 整体目标（把输出交给 file 检查）并未达成，仍应判失败。
_BASH_SEQ_SEPARATOR_RE = re.compile(r"&&|&|;|\n")

# 前置 wrapper：真实命令在其后（sudo env nohup time nice 等）。
_BASH_WRAPPER_CMDS = {
    "sudo", "env", "nohup", "time", "nice", "command", "exec",
    "builtin", "stdbuf", "timeout", "watch", "xargs",
}


def _bash_leading_command(command: str) -> str:
    """提取复合命令中第一个实际执行的命令名。

    跳过前置环境变量赋值（``VAR=value cmd …``）与常见 wrapper
    （``sudo env nohup …``），并剥离子 shell/花括号前缀（``(cd …``）。
    解析不出可信结果时返回空串（调用方据此放弃部分成功判定）。
    """
    if not command:
        return ""
    tokens = command.strip().split()
    for tok in tokens:
        if not tok or tok.startswith("#"):
            # 注释或空 token：其后再无真实命令。
            return ""
        head = tok.split("=", 1)[0]
        if "=" in tok and head.isidentifier():
            continue  # VAR=value 环境变量前缀
        if tok in _BASH_WRAPPER_CMDS:
            continue  # wrapper，继续找其后的真实命令
        return tok.lstrip("({[\"'")
    return ""


def _bash_cmdnotfound_is_partial(result_text: str, fn_args: dict) -> bool:
    """退出码 127 时判断是否为复合命令的部分成功（见上方注释）。"""
    command = str((fn_args or {}).get("command") or "")
    if not command or not _BASH_SEQ_SEPARATOR_RE.search(command):
        return False  # 单命令：command not found 就是彻底失败
    missing = set()
    for pattern in (_BASH_CMD_NOT_FOUND_RE, _ZSH_CMD_NOT_FOUND_RE):
        for match in pattern.finditer(result_text or ""):
            missing.add(match.group(1))
    if not missing:
        return False
    leading = _bash_leading_command(command)
    if not leading:
        return False
    return all(name != leading for name in missing)

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

    # web_search 单工具进行态固定显示搜索词。str() 防御：类型错误的
    # query（如数字）会被 L2 校验拦截回传，但 UI 摘要必须先不崩。
    if fn_name == "web_search":
        query = str(fn_args.get("query") or "").strip()
        return query if query else "Searching the web"

    # ---------- text_editor ----------
    # 注意：text_editor 不再声明 _description（意图）参数，摘要一律按
    # 「动作 + 文件名 + 行数差异」规范生成，模型即使惯性带上 _description
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

    custom_desc = _get_tool_description_from_args(fn_args)
    if custom_desc:
        return custom_desc

    # ---------- 特殊处理 ----------

    if fn_name == "fetch_url":
        url = str(fn_args.get("url") or "").strip()
        domain = extract_domain(url) if url else ""
        return f"Fetching from {domain}" if domain else "Fetching a page"

    if fn_name == "bash":
        cmd = str(fn_args.get("command") or "").strip()
        if cmd:
            short_cmd = cmd[:30] + "..." if len(cmd) > 30 else cmd
            return short_cmd
        return "Running command"

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

    if fn_name in ("ask_user", "message_user"):
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

    if not fn_name:
        # 流式占位条目（函数名尚未到达）没有可描述的工具名：
        # 返回空串，让调用方落到各自的通用进行态文本。
        return ""

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
        "message_user": "messaged you",
        "deliver_reply": "delivered the final reply",
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


# 有界快速字段扫描：只看头部前 4KB。command/path/query/url/_description
# 等控制字段在参数对象的最前面（大体积负载如 file_text / new_str 排在其
# 后），因此即使参数超过修复器的尺寸闸门、或 JSON 尚未闭合，也能拿到
# 进行态摘要所需的关键字段。
_FAST_SCAN_PREFIX_LEN = 4096
_FAST_FIELD_RE = re.compile(
    r'"(command|path|query|url|_description|_summary)"\s*:\s*"((?:[^"\\]|\\.)*)"'
)


def _fast_scan_fields(args_str: str) -> dict:
    """从（可能截断的）参数字符串头部快速提取控制字段（UI 摘要用途）。

    只匹配未转义的真实字段键——字符串值内部的引号在 JSON 里必然被
    转义（\\"），不会被误认成字段边界。同一字段取首次出现，反转义
    优先按 JSON 字符串字面量解析（与旧 _description 正则相同的策略，
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
    if result_content == _TOOL_TIMEOUT_MARKER:
        return True
    text = str(result_content or "").strip()
    lower = text.lower()
    if fn_name == "bash":
        # bash 的成功/失败以退出码为准；仅在明确看不到退出码时，再回退到错误前缀判断。
        m = re.search(r"Exit code:\s*(\d+)", text)
        if m:
            if m.group(1) == "0":
                return False
            # 127 = command not found。复合命令（a && b && c）中缺失的
            # 若只是中后段的辅助命令（如 `curl … && ls … && file …` 而
            # 容器未安装 file），按 && 短路语义，前面的主命令必然已成功
            # 执行——这是"部分成功"而非整体失败。判为非失败，避免 UI
            # 把已完成的实际工作显示成 "Tools failed"；完整输出（含
            # 错误文本与退出码）仍会回传给模型自行补救。
            return not (
                m.group(1) == "127" and _bash_cmdnotfound_is_partial(text, fn_args)
            )
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


def _generate_tool_summary_done(fn_name: str, fn_args: dict, result_content: str) -> str:
    """生成当前工具完成后的用户可见摘要。"""
    fn_args = fn_args or {}

    if fn_name == "web_search":
        query = str(fn_args.get("query") or "").strip()
        count = _extract_web_search_result_count(result_content)
        if query and count is not None:
            return f"{query} {count} result" if count == 1 else f"{query} {count} results"
        return "Searched the web"

    # ---------- text_editor ----------
    # text_editor 不再声明 _description（意图）参数：完成态摘要一律按
    # 「动作 + 文件名 + 行数差异」规范生成（本分支位于 custom_desc 检查
    # 之前，模型惯性携带的 _description 不会被采用）。
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

    if fn_name in ("ask_user", "message_user"):
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
        # 能走到这里且退出码非 0，说明已被判为"部分成功"（复合命令中
        # 辅助命令缺失，主体命令已完成）。摘要明确标出 partial，避免
        # 用户把带告警的成功误读为完全成功。
        m = re.search(r"Exit code:\s*(\d+)", str(result_content or ""))
        if m and m.group(1) != "0":
            return "Ran a command (partial success)"
        return "Ran a command"

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


