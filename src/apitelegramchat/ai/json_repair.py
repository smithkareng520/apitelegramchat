"""工具参数 JSON 的自动修复与精准诊断（Self-Correction 增强）。

背景
----
模型偶发返回畸形 JSON 工具参数（单引号、尾逗号、字符串内裸换行/未转义
引号、Python 字面量、截断等）。旧实现只把一句笼统的
"arguments were not valid JSON" 回传给模型，模型既不知道错在哪一列、
也不知道怎么改，只能盲猜重试，往往连续失败多轮。

本模块提供三层能力（主流修复管线：社区标准库优先，自研只做兜底）：

1. ``repair_json_arguments`` —— 双引擎保守自动修复：

   - 引擎 1：``json-repair`` **社区标准库**（LangChain / LlamaIndex 等
     框架修复 LLM JSON 输出的同款路线，覆盖面远大于任何自研修复器），
     未安装时静默跳过；
   - 引擎 2：自研语法级状态机（只做无歧义修复：双引号化、尾逗号删除、
     转义控制字符等），作为库缺失 / 修复失败时的兜底；
   - 修复结果必须通过 ``json.loads`` 且为 dict 才被接受。能在修复
     成功的场景直接省掉一整轮模型重试。截断的 JSON **不会**被
     猜测补全（除非显式 ``allow_close_truncated=True``，仅用于 UI 预览
     这类展示性场景），因为对被截断的命令参数做"猜补全再执行"可能
     产生危险语义（例如把 ``rm -rf /tmp/ju`` 补成完整字符串执行）。

2. ``build_invalid_arguments_envelope`` —— 修复失败时构建带完整诊断
   的可恢复错误信封：解析器原始报错（含行/列/字符位置）、出错位置
   的上下文摘录（带 ^ 指示符）、检测到的具体病因清单。信封本身是
   合法 JSON，可安全回传给 provider 而不会 400。

3. ``invalid_arguments_message`` —— 把诊断信息渲染成给模型看的、
   可操作的修复指引：先精确指出解析器报什么错、错在第几行第几列，
   再针对检测到的病因给出对应的修复规则，最后明确要求"重发同一
   个工具调用"。模型据此通常一轮即可自纠。

设计约束：本模块被置于参数解析的关键路径上，任何内部异常都不允许
向调用方冒泡——所有公开函数都有兜底 try/except。
"""
import json
import re
from typing import Optional

from apitelegramchat.utils import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 与 provider 交互的信封常量（原定义于 tool_summary.py，迁移至此作为单一
# 数据源；tool_summary 重新导出以保持旧导入路径兼容）。
# ---------------------------------------------------------------------------

# 流式调用偶发截断/拼接异常时，不能把原始坏字符串写回下一轮请求，否则
# 网关会在模型开始生成前直接 400。此标记本身是合法 JSON，并让执行层
# 返回可恢复错误。
_INVALID_TOOL_ARGUMENTS_KEY = "__apitelegram_invalid_tool_arguments__"
_INVALID_TOOL_ARGUMENTS_RAW_KEY = "raw_arguments_excerpt"
# 诊断字段（均为合法 JSON 标量/数组，直接放进信封）
_PARSE_ERROR_KEY = "parse_error"
_ERROR_CONTEXT_KEY = "error_context"
_DIAGNOSED_ISSUES_KEY = "diagnosed_issues"
_TRUNCATED_KEY = "looks_truncated"
# v2.5：流结束原因观测。None = 调用方无信息（保持旧行为）；
# "length" / "max_tokens" / "content_filter" = 确认被上限/过滤器切断；
# "" = 流消费完毕但从未见到终止事件（网关断流）；
# "stop" / "tool_calls" / "end_turn" / "tool_use" 等 = 正常结束。
_STREAM_FINISH_REASON_KEY = "stream_finish_reason"
_STREAM_CUT_KEY = "stream_cut"
_STREAM_CUT_CAUSE_KEY = "stream_cut_cause"
# 自动修复成功时注入 fn_args 的提示键（run_one 会 pop 掉并转成结果附注）
_JSON_REPAIR_NOTE_KEY = "__apitelegram_json_repair_note__"

# 信封里保留的原始参数摘录上限（与旧实现一致），错误消息里展示更短。
_RAW_EXCERPT_LIMIT = 2000
_MESSAGE_EXCERPT_LIMIT = 500
_MAX_ISSUES_IN_MESSAGE = 6
_MAX_FIXES_IN_NOTE = 4

# 流式 UI 预览兜底解析（_safe_parse_args）里允许进入修复器的最大长度：
# 预览每增长 20 字符就会重解析一次，纯 Python 状态机对超大参数反复
# 重写会阻塞事件循环，这里用尺寸闸门把最坏情况限制在一次 O(n)。
_STREAM_REPAIR_SIZE_LIMIT = 5000

_SMART_DOUBLE_QUOTES = ("\u201c", "\u201d")  # “ ”
_SMART_SINGLE_QUOTES = ("\u2018", "\u2019")  # ‘ ’
_SMART_TO_STRAIGHT = {
    "\u201c": '"', "\u201d": '"',
    "\u2018": "'", "\u2019": "'",
}

# 合法 JSON 转义的后继字符集
_VALID_JSON_ESCAPES = set('"\\/bfnrtu')

# python / js 字面量 → JSON 字面量
_LITERAL_MAP = {
    "True": "true", "False": "false", "None": "null",
    "NaN": "null", "Infinity": "null", "-Infinity": "null",
    "undefined": "null", "TRUE": "true", "FALSE": "false", "NULL": "null",
}

_MARKDOWN_FENCE_RE = re.compile(
    r"^```[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```[ \t]*$",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# 出错位置上下文
# ---------------------------------------------------------------------------

def _error_location_context(doc: str, pos: Optional[int], window: int = 48) -> str:
    """渲染解析器报错位置的上下文片段，形如::

        (line 3)
          {"command": "echo "hello"",
                             ^

    ``pos`` 为 ``json.JSONDecodeError.pos``。行过长时以出错列为中心截断
    （窗口两侧加省略号）。任何异常都返回空串。
    """
    try:
        if not isinstance(doc, str) or pos is None or pos < 0 or pos > len(doc):
            return ""
        line_start = doc.rfind("\n", 0, pos) + 1
        line_end = doc.find("\n", pos)
        if line_end == -1:
            line_end = len(doc)
        line_no = doc.count("\n", 0, pos) + 1
        col = pos - line_start
        line = doc[line_start:line_end]
        if not line.strip():
            # 空行（例如报错指向换行符本身）：展示下一行更有用。
            nxt = doc.find("\n", pos + 1)
            nxt = len(doc) if nxt == -1 else nxt
            line = doc[pos + 1:nxt]
            col = 0
            if not line.strip():
                return f"(line {line_no})"
        if len(line) > window * 2:
            lo = max(0, col - window)
            hi = min(len(line), col + window)
            prefix = "…" if lo > 0 else ""
            suffix = "…" if hi < len(line) else ""
            line = prefix + line[lo:hi] + suffix
            col_disp = col - lo + len(prefix)
        else:
            col_disp = col
        caret = " " * col_disp + "^"
        return f"(line {line_no})\n  {line}\n  {caret}"
    except Exception:
        logger.debug("_error_location_context 内部忽略的异常", exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# 字符级、字符串感知的 JSON 重写器
# ---------------------------------------------------------------------------

def _rewrite_jsonish(raw: str, allow_close: bool) -> tuple[str, list, bool]:
    """对 JSON 风格字符串做保守语法修复，返回 ``(重写结果, 修复说明, 是否截断)``。

    修复项（全部仅在字符串边界外/内正确区分的前提下进行）：
    - markdown 代码围栏 / 对象前后杂质文本剥离（由调用方 _extract_jsonish 完成）
    - 智能引号 → 直引号
    - 单引号字符串 → 双引号字符串（内部引号正确转义）
    - 未加引号的标识符键 → 加引号
    - 尾逗号删除
    - 字符串内裸控制字符 → ``\\n`` / ``\\t`` / ``\\uXXXX``
    - 字符串内未转义双引号 → ``\\"``（仅当该引号后不是合法的 JSON 后继符）
    - 非法反斜杠转义 → 双反斜杠
    - Python/JS 字面量 True/False/None/NaN/undefined → true/false/null
    - ``//`` 与 ``/* */`` 注释删除
    - （allow_close=True 时）补全未闭合的字符串与括号

    截断判定：扫描到末尾仍处于字符串内或仍有未闭合括号 → truncated=True。
    allow_close=False 时保持原样不猜测补全（安全优先）。
    """
    fixes: list = []
    out: list = []
    stack: list = []
    in_string = False
    quote_char = '"'
    i = 0
    n = len(raw)

    def _last_non_ws_idx() -> int:
        j = len(out) - 1
        while j >= 0 and out[j] in " \t\r\n":
            j -= 1
        return j

    while i < n:
        ch = raw[i]

        if not in_string:
            if ch in " \t\r\n":
                out.append(ch)
                i += 1
                continue
            if ch in "{[":
                out.append(ch)
                stack.append(ch)
                i += 1
                continue
            if ch in "}]":
                # 尾逗号：闭括号前最后一个非空白输出是逗号 → 删除
                j = _last_non_ws_idx()
                if j >= 0 and out[j] == ",":
                    del out[j:]
                    if "removed trailing comma" not in fixes:
                        fixes.append("removed trailing comma before a closing bracket")
                if stack and stack[-1] == ("{" if ch == "}" else "["):
                    stack.pop()
                    out.append(ch)
                elif stack:
                    # 括号错配：丢弃该闭括号并记录（重写结果可能仍不可解析，
                    # 但诊断信息已经足够模型自纠）
                    if "dropped mismatched closing bracket" not in fixes:
                        fixes.append("dropped mismatched closing bracket")
                else:
                    # 栈已空却遇到闭括号：大概率是对象后面的杂质文本，停止重写
                    if "stopped at extra text after the JSON object" not in fixes:
                        fixes.append("stopped at extra text after the JSON object")
                    break
                i += 1
                continue
            if ch in ",:":
                out.append(ch)
                i += 1
                continue
            if ch == '"':
                in_string = True
                quote_char = '"'
                out.append(ch)
                i += 1
                continue
            if ch == "'" or ch in _SMART_SINGLE_QUOTES or ch in _SMART_DOUBLE_QUOTES:
                # 单引号 / 智能引号开字符串 → 双引号
                in_string = True
                quote_char = "'" if (ch == "'" or ch in _SMART_SINGLE_QUOTES) else '"'
                out.append('"')
                if "converted single/smart-quoted strings to double quotes" not in fixes:
                    fixes.append("converted single/smart-quoted strings to double quotes")
                i += 1
                continue
            if ch == "/" and i + 1 < n and raw[i + 1] == "/":
                j = raw.find("\n", i)
                i = n if j == -1 else j
                if "removed // comment" not in fixes:
                    fixes.append("removed // comment")
                continue
            if ch == "/" and i + 1 < n and raw[i + 1] == "*":
                j = raw.find("*/", i + 2)
                i = n if j == -1 else j + 2
                if "removed /* */ comment" not in fixes:
                    fixes.append("removed /* */ comment")
                continue
            if ch.isalpha() or ch == "_" or ch == "$":
                j = i
                while j < n and (raw[j].isalnum() or raw[j] in "_$"):
                    j += 1
                word = raw[i:j]
                # 前瞻冒号 → 未加引号的键
                k = j
                while k < n and raw[k] in " \t":
                    k += 1
                if k < n and raw[k] == ":":
                    out.append('"' + word + '"')
                    if f"quoted unquoted key '{word[:24]}'" not in fixes and \
                            "quoted unquoted keys" not in fixes:
                        fixes.append("quoted unquoted keys (e.g. %r)" % word[:24])
                    i = j
                    continue
                if word in _LITERAL_MAP:
                    out.append(_LITERAL_MAP[word])
                    if f"converted Python/JS literal {word} to {_LITERAL_MAP[word]}" not in fixes:
                        fixes.append(
                            "converted Python/JS literals (True/False/None/NaN) to JSON (true/false/null)")
                    i = j
                    continue
                if word in ("true", "false", "null"):
                    out.append(word)
                    i = j
                    continue
                # 裸标识符值：加引号（保守处理，可能因上下文歧义修复失败，
                # 失败时诊断信息兜底）
                out.append('"' + word + '"')
                if "quoted bare identifier values" not in fixes:
                    fixes.append("quoted bare identifier values")
                i = j
                continue
            # 其余（数字、-、.、e、E、+ 等）原样通过
            out.append(ch)
            i += 1
            continue

        # ===== 字符串内 =====
        qc = quote_char
        if ch == "\\":
            if i + 1 < n:
                nxt = raw[i + 1]
                if nxt in _VALID_JSON_ESCAPES:
                    if qc == "'" and nxt == "'":
                        # \' 在单引号串里表示字面单引号
                        out.append("'")
                    elif qc == "'" and nxt == '"':
                        out.append('\\"')
                    else:
                        out.append("\\" + nxt)
                    i += 2
                    continue
                if qc == "'" and nxt == "'":
                    out.append("'")
                    i += 2
                    continue
                # 非法转义 → 双反斜杠（Windows 路径 C:\Users 等）
                out.append("\\\\" + nxt)
                if "escaped invalid backslash sequences (e.g. Windows paths)" not in fixes:
                    fixes.append("escaped invalid backslash sequences (e.g. Windows paths)")
                i += 2
                continue
            # 末尾孤立反斜杠：丢弃
            i += 1
            continue
        if ch == qc:
            # 候选闭合引号：后继非空白字符必须是结构符（,:}]）或 EOF 才算真闭合，
            # 否则视为字符串内未转义引号 → 转义
            k = i + 1
            while k < n and raw[k] in " \t\r\n":
                k += 1
            nxt_ch = raw[k] if k < n else ""
            if nxt_ch == "" or nxt_ch in ",:}]":
                in_string = False
                out.append('"')
            else:
                out.append('\\"' if qc == '"' else '"')
                if "escaped unescaped double quotes inside string values" not in fixes:
                    fixes.append("escaped unescaped double quotes inside string values")
            i += 1
            continue
        if ch == '"' and qc == "'":
            # 双引号串外的 '"' 在单引号串里是内容 → 转义保留
            out.append('\\"')
            i += 1
            continue
        if ch in _SMART_TO_STRAIGHT:
            rep = _SMART_TO_STRAIGHT[ch]
            if rep == '"':
                out.append('\\"')
                if "normalized smart quotes (\u201c\u201d) inside strings" not in fixes:
                    fixes.append("normalized smart quotes inside strings")
            else:
                out.append("'")
                if "normalized smart quotes (\u2018\u2019) inside strings" not in fixes:
                    fixes.append("normalized smart quotes inside strings")
            i += 1
            continue
        if ord(ch) < 0x20 or ch == "\x7f":
            esc = {"\n": "\\n", "\t": "\\t", "\r": "\\r"}.get(ch)
            if esc is not None:
                out.append(esc)
                if "escaped raw line breaks/tabs inside string values" not in fixes:
                    fixes.append("escaped raw line breaks/tabs inside string values")
            else:
                out.append("\\u%04x" % ord(ch))
                if "escaped raw control characters inside string values" not in fixes:
                    fixes.append("escaped raw control characters inside string values")
            i += 1
            continue
        out.append(ch)
        i += 1
        continue

    truncated = in_string or bool(stack)
    if truncated and allow_close:
        if in_string:
            out.append('"')
            if "closed an unterminated string" not in fixes:
                fixes.append("closed an unterminated string")
        while stack:
            out.append(stack.pop())
        if "auto-closed unclosed brackets" not in fixes:
            fixes.append("auto-closed unclosed brackets")
        truncated = False
        # 闭完后若以 : 或 , 结尾（悬空键/元素）→ 补 null 占位
        j = _last_non_ws_idx()
        if j >= 0 and out[j] in ":,":
            out.append("null")
    elif truncated:
        if "JSON appears truncated (unterminated string or unclosed brackets)" not in fixes:
            fixes.append("JSON appears truncated (unterminated string or unclosed brackets)")

    return "".join(out), fixes, truncated


def _extract_jsonish(raw: str) -> tuple[str, list]:
    """剥离 markdown 围栏与对象前后的杂质文本，返回 (候选 JSON, 修复说明)。"""
    fixes: list = []
    s = raw.strip()
    m = _MARKDOWN_FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
        fixes.append("stripped markdown code fence around the JSON")
    start = s.find("{")
    if start == -1:
        # 没有对象起始：看看是不是数组等其他形式，交由上层判定
        return s, fixes
    if start > 0:
        s = s[start:]
        fixes.append("stripped non-JSON text before the object")
    # 平衡扫描找第一个完整对象（字符串感知）
    depth = 0
    in_str = False
    esc = False
    for idx in range(len(s)):
        ch = s[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    if idx + 1 < len(s.rstrip()):
                        s = s[: idx + 1]
                        fixes.append("stripped non-JSON text after the object")
                    return s, fixes
    # 未找到平衡闭合（截断）
    return s, fixes


# ---------------------------------------------------------------------------
# 公开 API：修复 / 诊断 / 消息渲染
# ---------------------------------------------------------------------------

# json-repair 社区标准库（可选依赖）的函数句柄缓存：
# None = 尚未探测；False = 不可用（未安装）；其余 = 可调用的 repair_json。
# 流式预览路径每 20 字符就会重解析一次，import 探测结果必须缓存，
# 避免每次调用都付出一次 import 开销。
_LIB_REPAIR_JSON = None


def _repair_with_library(candidate: str) -> Optional[dict]:
    """用 json-repair 社区标准库修复并解析为 dict。

    - 库不可用（未安装 / 导入失败）时返回 ``None``，调用方走自研兜底
      引擎，行为与旧版完全一致；
    - 修复后必须是 dict（工具参数是对象），否则视为失败；
    - 任何异常都吞掉——库是增强项，绝不阻塞关键路径。
    """
    global _LIB_REPAIR_JSON
    try:
        if _LIB_REPAIR_JSON is False:
            return None
        if _LIB_REPAIR_JSON is None:
            # 注意：本模块名为 apitelegramchat.ai.json_repair，绝对导入
            # 顶层 json_repair 解析到的是 site-packages 里的社区库，
            # 不会命中自身。
            from json_repair import repair_json as _fn
            _LIB_REPAIR_JSON = _fn
        repaired_text = _LIB_REPAIR_JSON(candidate)
        if not isinstance(repaired_text, str) or not repaired_text.strip():
            return None
        parsed = json.loads(repaired_text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def repair_json_arguments(
        raw: str, *, allow_close_truncated: bool = False,
) -> tuple[Optional[dict], dict]:
    """尝试把畸形 JSON 参数修复为 dict（双引擎：社区标准库优先）。

    返回 ``(repaired, info)``：
    - ``repaired``：修复成功且解析为 dict 时返回该 dict，否则 None；
    - ``info``：``{"fixes": [..], "truncated": bool, "note": str}``，
      无论成败都携带已尝试的修复说明（用于日志与诊断）。

    引擎优先级（主流做法：先社区标准库，自研状态机只作兜底）：

    1. **截断安全预检**：先用现有扫描器判定截断。执行路径
       （``allow_close_truncated=False``）命中截断一律拒绝——
       json-repair 库默认会补全截断输入，但「猜补全再执行」可能产生
       危险语义（把 ``rm -rf /tmp/ju" 补成完整命令执行），因此安全
       闸门必须在库之前。
    2. **json-repair 社区标准库**（可选依赖）：覆盖面远大于自研状态机
       （未转义引号、嵌套结构、注释、尾逗号等），未安装时静默跳过。
    3. **自研保守状态机**：原有行为不变，作为库缺失 / 修复失败时的
       兜底引擎。

    任何内部异常都兜底返回 ``(None, info)``。
    """
    info = {"fixes": [], "truncated": False, "note": ""}
    try:
        if not isinstance(raw, str) or not raw.strip():
            info["note"] = "arguments were empty"
            return None, info
        candidate, fixes = _extract_jsonish(raw)
        info["fixes"] = list(fixes)
        if not candidate.strip():
            info["note"] = "no JSON object found in arguments"
            return None, info
        rewritten, rewrite_fixes, truncated = _rewrite_jsonish(
            candidate, allow_close=allow_close_truncated)
        for f in rewrite_fixes:
            if f not in info["fixes"]:
                info["fixes"].append(f)
        info["truncated"] = truncated
        if truncated and not allow_close_truncated:
            info["note"] = "arguments JSON is truncated / incomplete"
            return None, info
        # 引擎 1：json-repair 社区标准库（未安装时返回 None 走兜底引擎）。
        lib_repaired = _repair_with_library(candidate)
        if isinstance(lib_repaired, dict):
            note = "repaired with the json-repair library"
            if note not in info["fixes"]:
                info["fixes"].append(note)
            return lib_repaired, info
        # 引擎 2：自研保守状态机的重写结果（原有行为）。
        try:
            parsed = json.loads(rewritten)
        except (json.JSONDecodeError, ValueError) as exc:
            info["note"] = f"still unparseable after repair: {exc}"
            return None, info
        if not isinstance(parsed, dict):
            # 修复后是数组/标量：不算修复成功（参数必须是对象）
            info["note"] = f"repaired JSON is a {type(parsed).__name__}, not an object"
            return None, info
        return parsed, info
    except Exception as exc:  # noqa: BLE001 —— 关键路径兜底，绝不冒泡
        logger.warning("repair_json_arguments 内部异常: %s", exc, exc_info=True)
        info["note"] = f"repair crashed: {exc}"
        return None, info


def _kind_of_valid_json(parsed) -> Optional[str]:
    """json.loads 成功但非 dict 时，返回可读的类型描述。"""
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
    return None


def _parser_error_text(exc: Optional[Exception], raw: str) -> str:
    """解析器报错原文（含行/列/字符位置）。"""
    try:
        if isinstance(exc, json.JSONDecodeError):
            return str(exc)
        if exc is not None:
            return f"{type(exc).__name__}: {exc}"
        # 没有异常对象时重新解析一次拿到报错
        try:
            json.loads(raw)
        except json.JSONDecodeError as e:
            return str(e)
        return "arguments were not valid JSON"
    except Exception:
        return "arguments were not valid JSON"


def _finish_reason_cut_info(stream_finish_reason: Optional[str]) -> tuple:
    """把流结束原因归类为「是否异常截断 + 面向模型的原因描述」。

    返回 ``(is_cut, cause)``。仅基于积极证据判定截断：
    - ``length`` / ``max_tokens``：输出 token 上限（OpenAI / Anthropic 各自的拼写）；
    - ``content_filter``：内容过滤器拦停；
    - ``""``：流被完整消费但从未出现终止事件——正常兼容端一定会发
      finish_reason，缺失本身就是断流证据（网关提前关闭 / 连接中断）；
    - ``None``：调用方没有该信息（旧行为，不下结论）；
    - 其余（stop / tool_calls / end_turn / tool_use…）：正常结束，
      参数写坏了是模型自己的问题——这同样是有价值的定向信息。
    """
    if stream_finish_reason is None:
        return False, ""
    fr = str(stream_finish_reason).strip().lower()
    if not fr:
        return True, "the stream ended without a finish_reason termination event (connection or gateway cutoff)"
    if fr in ("length", "max_tokens"):
        return True, "the output token limit was reached before the tool call finished"
    if fr == "content_filter":
        return True, "the response was stopped by the content filter"
    return False, ""


def build_invalid_arguments_envelope(
        raw: str, *, exc: Optional[Exception] = None, arg_kind: Optional[str] = None,
        stream_finish_reason: Optional[str] = None,
) -> dict:
    """修复失败后构建带完整诊断的可恢复错误信封（合法 JSON dict）。

    - ``raw``：模型原始参数字符串；
    - ``exc``：调用方捕获的解析异常（可选，缺省时重新解析获取）；
    - ``arg_kind``：当 JSON 合法但顶层不是对象时（如 "a JSON array"），
      走独立 reason，不进行修复尝试。
    """
    try:
        raw = raw if isinstance(raw, str) else str(raw or "")
        excerpt = raw[:_RAW_EXCERPT_LIMIT]
        cut, cut_cause = _finish_reason_cut_info(stream_finish_reason)
        if arg_kind:
            reason = f"arguments must be a JSON object, but the value parsed as {arg_kind}"
            return {
                _INVALID_TOOL_ARGUMENTS_KEY: reason,
                _INVALID_TOOL_ARGUMENTS_RAW_KEY: excerpt,
                _PARSE_ERROR_KEY: reason,
                _DIAGNOSED_ISSUES_KEY: [
                    f"top-level value is {arg_kind}; tool arguments must be an "
                    "object like {\"command\": \"...\"}",
                ],
                _ERROR_CONTEXT_KEY: excerpt[:200],
                _TRUNCATED_KEY: False,
            }
        if not raw.strip():
            reason = "arguments were empty — provide the required arguments as a JSON object"
            issues = ["arguments field was empty or whitespace-only"]
            truncated = False
            # v2.5：带 id/name 的工具调用不会自愿发空参数；若流结束原因
            # 显示异常截断（上限/断流），根因几乎必是「参数还没开始生成就
            # 被切断」——明说根因并标记截断，让分块降级指引生效。
            if cut:
                reason = (
                    "arguments were empty — the response stream was cut off before any "
                    f"argument content was generated ({cut_cause})"
                )
                issues.append(f"truncated: no argument content arrived — {cut_cause}")
                truncated = True
            env = {
                _INVALID_TOOL_ARGUMENTS_KEY: reason,
                _INVALID_TOOL_ARGUMENTS_RAW_KEY: "",
                _PARSE_ERROR_KEY: reason,
                _DIAGNOSED_ISSUES_KEY: issues,
                _ERROR_CONTEXT_KEY: "",
                _TRUNCATED_KEY: truncated,
            }
            if stream_finish_reason is not None:
                env[_STREAM_FINISH_REASON_KEY] = str(stream_finish_reason)
                env[_STREAM_CUT_KEY] = bool(cut)
                if cut:
                    env[_STREAM_CUT_CAUSE_KEY] = cut_cause
            return env
        parser_error = _parser_error_text(exc, raw)
        # 复用修复器的重写扫描来收集病因（不需要修复成功）
        candidate, _ = _extract_jsonish(raw)
        _, rewrite_fixes, truncated = _rewrite_jsonish(candidate, allow_close=False)
        issues: list = []
        for f in rewrite_fixes:
            if f not in issues:
                issues.append(f)
        # v2.5：流结束原因佐证——finish_reason=length 与截断判定互相印证，
        # 把根因写进病因清单（含 "cut off"，供消息层渲染专属段）。
        if cut:
            marker = f"response was cut off mid-generation — {cut_cause}"
            if marker not in issues:
                issues.append(marker)
        # 解析器报错位置（来自原始 raw，而非重写结果）
        pos = getattr(exc, "pos", None)
        if pos is None:
            try:
                json.loads(raw)
            except json.JSONDecodeError as e:
                pos = e.pos
        context = _error_location_context(raw, pos) if pos is not None else ""
        reason = "arguments were not valid JSON (auto-repair failed)"
        if truncated:
            reason = "arguments were not valid JSON (appear truncated / incomplete)"
        elif cut:
            reason = f"arguments were not valid JSON ({cut_cause})"
        env = {
            _INVALID_TOOL_ARGUMENTS_KEY: reason,
            _INVALID_TOOL_ARGUMENTS_RAW_KEY: excerpt,
            _PARSE_ERROR_KEY: parser_error,
            _ERROR_CONTEXT_KEY: context[:400],
            _DIAGNOSED_ISSUES_KEY: issues[:10],
            _TRUNCATED_KEY: bool(truncated),
        }
        if stream_finish_reason is not None:
            env[_STREAM_FINISH_REASON_KEY] = str(stream_finish_reason)
            env[_STREAM_CUT_KEY] = bool(cut)
            if cut:
                env[_STREAM_CUT_CAUSE_KEY] = cut_cause
        return env
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_invalid_arguments_envelope 内部异常: %s", exc, exc_info=True)
        return {
            _INVALID_TOOL_ARGUMENTS_KEY: "arguments were not valid JSON",
            _INVALID_TOOL_ARGUMENTS_RAW_KEY: (raw or "")[:_RAW_EXCERPT_LIMIT] if isinstance(raw, str) else "",
            _PARSE_ERROR_KEY: "arguments were not valid JSON",
            _ERROR_CONTEXT_KEY: "",
            _DIAGNOSED_ISSUES_KEY: [],
            _TRUNCATED_KEY: False,
        }


# 诊断 issue → 面向模型的修复规则
_ISSUE_RULES = (
    ("single/smart-quoted", 'Use straight double quotes " for all keys and string values; single quotes are not valid JSON.'),
    ("unescaped double quotes", 'Escape every double quote INSIDE a string value as \\" .'),
    ("raw line breaks", "Write newlines inside string values as the two characters \\n — never literal line breaks."),
    ("raw control", "Escape control characters inside strings (\\n, \\t, \\uXXXX)."),
    ("trailing comma", "Remove trailing commas before } or ] (JSON does not allow them)."),
    ("unquoted key", "Put double quotes around every key: {\"command\": ...}."),
    ("bare identifier", "String values must be quoted: {\"command\": \"rm -rf /\"}, not {\"command\": rm -rf /}."),
    ("literal", "Use JSON literals only: true / false / null (not True / False / None / NaN / undefined)."),
    ("backslash", "Escape each literal backslash as \\\\ (e.g. C:\\\\Users, regex \\\\d)."),
    ("comment", "Remove comments — JSON has no // or /* */ comments."),
    ("truncated", "The arguments JSON is incomplete (cut off). Reissue the tool call with the COMPLETE arguments."),
    ("fence", "Do not wrap the JSON in markdown code fences (```) or add prose around it."),
    ("text before/after", "Send only the JSON object itself, no prose before or after it."),
    ("unterminated string", "Close every string with a closing double quote."),
    ("mismatched closing", "Match brackets correctly — every { must close with } and every [ with ]."),
)


def _rules_for_issues(issues: list) -> list:
    rules: list = []
    for key, rule in _ISSUE_RULES:
        if any(key.lower() in str(issue).lower() for issue in issues):
            if rule not in rules:
                rules.append(rule)
    if not rules:
        rules.append(
            'Re-check quoting/escaping: double quotes for keys and strings, '
            '\\" for inner quotes, \\\\ for backslashes, \\n for newlines, no trailing commas.'
        )
    return rules[: _MAX_ISSUES_IN_MESSAGE]


def invalid_arguments_message(fn_name: str, fn_args: dict) -> str:
    """把诊断信封渲染成给模型的、可操作的错误反馈。

    消息以 ``Error:`` 开头（供 _tool_result_is_failure 识别失败状态），
    首行保持稳定（供错误连击熔断做签名归一化），随后依次给出：
    解析器原始报错 → 出错位置上下文（^ 指示）→ 病因清单 → 原始参数
    摘录 → 针对性修复规则与重发指引。
    """
    try:
        fn_args = fn_args if isinstance(fn_args, dict) else {}
        reason = str(fn_args.get(_INVALID_TOOL_ARGUMENTS_KEY) or "arguments were not valid JSON")
        parse_error = str(fn_args.get(_PARSE_ERROR_KEY) or reason)
        context = str(fn_args.get(_ERROR_CONTEXT_KEY) or "")
        issues = [str(x) for x in (fn_args.get(_DIAGNOSED_ISSUES_KEY) or []) if str(x).strip()]
        truncated = bool(fn_args.get(_TRUNCATED_KEY))
        raw_excerpt = str(fn_args.get(_INVALID_TOOL_ARGUMENTS_RAW_KEY) or "")
        raw_len = len(raw_excerpt)

        first_line = (
            f"Error: tool {fn_name} was NOT executed: its arguments are not valid JSON, "
            "so they could not be parsed into tool input."
        )
        if truncated:
            first_line += " The JSON also appears truncated (unexpected end of input)."
        elif "must be a JSON object" in reason:
            first_line = (
                f"Error: tool {fn_name} was NOT executed: its arguments parsed as valid JSON, "
                "but the top-level value is not a JSON object."
            )

        parts = [first_line, ""]
        parts.append(f"[Parser error] {parse_error}")
        # v2.5：流结束原因专属段——把「传输层被切断」与「模型写坏了 JSON"
        # 两种根因彻底分开。前者重引号没用，必须缩输出/分块；后者才是
        # 语法修复路径。正常结束（stop/tool_calls）时明确告诉模型
        # 「问题在你自己的 JSON」，避免误归因。
        stream_fr = fn_args.get(_STREAM_FINISH_REASON_KEY)
        stream_cut = bool(fn_args.get(_STREAM_CUT_KEY))
        if stream_cut:
            cause = str(fn_args.get(_STREAM_CUT_CAUSE_KEY) or "the stream ended before the arguments were complete")
            parts.append(f"[Why: the response stream was cut off] finish_reason={stream_fr or 'not reported'} — {cause}.")
            parts.append(
                "This is NOT a JSON syntax problem: the arguments were never fully generated, so "
                "re-quoting the same oversized call will fail the same way."
            )
            parts.append(
                "- Shorten the text/thinking you emit BEFORE the tool call — start tool calls early in the response."
            )
            parts.append(
                "- Split large payloads into several smaller calls (each argument well under ~4KB)."
            )
        elif stream_fr:
            parts.append(
                f"[Stream status] The response ended normally (finish_reason={stream_fr}); "
                "the malformed JSON is on your side — fixing the syntax is the right fix."
            )
        if context:
            parts.append("[Where the parser stopped]")
            parts.append(context)
        if issues:
            parts.append("[Problems detected in your arguments]")
            for issue in issues[:_MAX_ISSUES_IN_MESSAGE]:
                parts.append(f"- {issue}")
        if raw_excerpt:
            shown = raw_excerpt[:_MESSAGE_EXCERPT_LIMIT]
            more = "" if raw_len <= _MESSAGE_EXCERPT_LIMIT else f" (first {_MESSAGE_EXCERPT_LIMIT} of {raw_len} chars)"
            parts.append(f"[Your raw arguments]{more}")
            parts.append(shown)
        parts.append("[How to fix] Reissue the SAME tool call (" + str(fn_name) + ") with corrected arguments:")
        for rule in _rules_for_issues(issues + ([reason] if not issues else [])):
            parts.append(f"- {rule}")
        if truncated:
            # v2.4：截断专属补充指引。诊断层无法从截断的参数里区分"偶发流断连"
            # 还是"单参数载荷超限"，但"重发完整参数"对后者是死循环陷阱——
            # 同样的巨参数会以同样的方式再次被切断。因此明确给出重试上限与
            # 分块降级策略，避免模型在超大 file_text/new_str 上反复撞墙。
            parts.append(
                "[If the arguments were cut off mid-generation] A single oversized "
                "argument (large file_text / new_str / long command) often exceeds the "
                "output limit and CANNOT be delivered in one call no matter how you "
                "re-quote it. Reissue the complete call ONCE; if truncation repeats, "
                "switch strategy immediately:"
            )
            parts.append(
                "- Split the payload: create the file with a small skeleton, then add "
                "the remaining content through several smaller insert / str_replace "
                "calls (keep each argument well under ~4KB)."
            )
            parts.append(
                "- Or write large content via a bash heredoc / script file in chunks."
            )
            parts.append(
                "In the truncated case you MAY adjust how the content is delivered "
                "(chunked), as long as the task itself is unchanged."
            )
        else:
            parts.append(
                "Do not change the tool or the task — only fix the JSON syntax of the arguments."
            )
        return "\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("invalid_arguments_message 内部异常: %s", exc, exc_info=True)
        return (
            f"Error: tool {fn_name} was not executed because the model returned malformed JSON "
            "arguments. Reissue the same tool call with a valid JSON object."
        )


def repair_note_for_result(fixes: list) -> str:
    """修复成功后附加到工具结果末尾的提示（帮助模型知悉并学习）。"""
    try:
        shown = "; ".join(str(f) for f in (fixes or [])[:_MAX_FIXES_IN_NOTE])
        note = (
            "Note: the arguments of this tool call were malformed JSON and were "
            "auto-repaired before execution"
        )
        if shown:
            note += f" (fixes applied: {shown})"
        note += (
            ". Please verify the result matches your intent, and emit strictly "
            "valid JSON (double quotes, escaped inner quotes/backslashes, "
            "\\n for newlines, no trailing commas) in future tool calls."
        )
        return note
    except Exception:  # noqa: BLE001
        return ""
