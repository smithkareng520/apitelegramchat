"""子 agent 进度预览渲染：把 status_text 解析成结构化 Rich Message 卡片。

从 tool_call_loop.py 拆出（该文件的职责应是"并行执行工具调用"，子 agent
进度文案的正则解析/渲染是独立的表现层关切，与工具调用编排耦合在同一
文件里会降低两者各自的可读性——修改熔断/超时等执行逻辑时不必经过这段
纯文本处理代码，反之亦然）。

子 agent 在 subagent_tool.py 的 _subagent_agentic_loop 里通过 _report
推送的 status_text 是一组带固定模式的中文短句（"第 X/Y 轮：LLM 思考中…
（已耗时 Xs）"、"完成：X 轮，N 次工具调用，Xs" 等）。本模块把它们解析成
结构化字段，渲染成 Telegram Rich Message 块级 HTML，让用户能直接读到
当前阶段、当前轮数、已耗时、正在执行的工具名——而不是一行被 italic 化、
被截断到 300 token 的灰色状态句。
"""
import re
import html


# 顺序敏感：先匹配「完成 / 结束 / 超时 / 失败」再匹配「执行工具」、
# 最后兜底「启动」。
_SUBAGENT_PROGRESS_PHASE_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("done",     re.compile(r"^完成[:：]")),
    ("terminal", re.compile(r"^结束[:：]")),
    ("timeout",  re.compile(r"整体超时|LLM 调用超时|轮.*超时")),
    ("error",    re.compile(r"失败|解析失败")),
    ("tools",    re.compile(r"执行工具")),
    ("thinking", re.compile(r"LLM 思考中")),
    ("start",    re.compile(r"^启动子")),
]

_SUBAGENT_ROUND_RE = re.compile(r"第\s*(\d+)\s*[/／]\s*(\d+)\s*轮")
_SUBAGENT_PLAIN_ROUND_RE = re.compile(r"第\s*(\d+)\s*轮")
_SUBAGENT_ELAPSED_RE = re.compile(r"已耗时\s*([0-9.]+)\s*[s秒]")
# 「完成：X 轮，N 次工具调用，Xs」与「结束：…（X 轮，N 次工具调用）」
# 共享同一个轮次+工具调用次数模式；秒数仅在完成行出现，故设为可选。
_SUBAGENT_TOTAL_TIME_RE = re.compile(
    r"(\d+)\s*轮[，,]\s*(\d+)\s*次工具调用"
    r"(?:[，,]\s*([0-9.]+)\s*[s秒])?"
)
_SUBAGENT_TOOL_NAMES_RE = re.compile(r"执行工具\s*(.+?)\s*[（(]?\s*已耗时")
_SUBAGENT_MODEL_RE = re.compile(r"模型\s*([^，,（(]+?)\s*[，,]")


def subagent_progress_phase(status_text: str) -> str:
    """从 status_text 里抽出当前阶段，用于节流决策（同一阶段内合并刷新）。"""
    if not status_text:
        return "unknown"
    for phase, pattern in _SUBAGENT_PROGRESS_PHASE_PATTERNS:
        if pattern.search(status_text):
            return phase
    return "unknown"


def format_subagent_progress_html(status_text: str) -> str:
    """把子 agent 的中文状态短句渲染成结构化富文本卡片。

    返回的 HTML 片段由若干 ``<p>`` 块级元素组成，可直接嵌入工具卡片的
    ``<details>``。自由文本字段（模型名、工具名、未结构化兜底文本）均经
    ``html.escape`` 转义，避免子 agent 状态文案中的裸 HTML 字符（例如
    工具名恰好含 ``<``/``>``）破坏 Rich Message 结构。数字字段（轮次、
    耗时）来自 ``\\d+``/``[0-9.]+`` 正则捕获，天然不含 HTML 元字符，
    直接使用无需转义。

    注意：不要在这里用 ``convert_markdown_to_telegram_html`` 替代
    ``html.escape``——前者只在检测到 Markdown 语法时才转义，纯文本
    （包括裸 ``<script>`` 这类内容）会被判定为"无 Markdown"而原样直通，
    对本函数这种"字段不应包含富文本"的场景无法提供转义保证。
    """
    text = status_text or "正在执行…"
    phase = subagent_progress_phase(text)
    phase_meta = {
        "start":    ("🤖", "启动子 agent"),
        "thinking": ("🧠", "LLM 思考中"),
        "tools":    ("🔧", "正在调用工具"),
        "done":     ("✅", "子 agent 已完成"),
        "terminal": ("⚠️", "已结束"),
        "timeout":  ("⏱️", "超时"),
        "error":    ("❌", "出错"),
        "unknown":  ("…",  "进行中"),
    }.get(phase, ("…", "进行中"))
    icon, phase_label = phase_meta

    round_match = _SUBAGENT_ROUND_RE.search(text)
    plain_round_match = _SUBAGENT_PLAIN_ROUND_RE.search(text)
    elapsed_match = _SUBAGENT_ELAPSED_RE.search(text)
    total_match = _SUBAGENT_TOTAL_TIME_RE.search(text)
    tool_names_match = _SUBAGENT_TOOL_NAMES_RE.search(text)
    model_match = _SUBAGENT_MODEL_RE.search(text)

    rows = []
    if model_match:
        # 模型名/工具名来自子 agent 自由拼接的状态文案，理论上可能间接
        # 受模型输出影响（如 MCP 动态工具名）。用 html.escape 而非
        # convert_markdown_to_telegram_html：后者只在检测到 Markdown 语法
        # 时才转义，纯文本（含裸 "<script>" 这类内容）会被判定为"无
        # Markdown"而原样直通——对这类不应包含富文本的短字段，
        # html.escape 是唯一能保证转义生效的方式。
        rows.append(f"<b>模型</b>：{html.escape(model_match.group(1).strip())}")
    if round_match:
        rows.append(
            f"<b>轮次</b>：{round_match.group(1)} / {round_match.group(2)}"
        )
    elif plain_round_match and phase not in ("done", "terminal"):
        rows.append(f"<b>轮次</b>：{plain_round_match.group(1)}")
    if tool_names_match:
        # 子 agent 推送的 status_text 在工具名后追加了「…」，原样展示会
        # 把省略号当成工具名一部分。统一去除尾部省略号 / 点号。
        raw_names = tool_names_match.group(1).strip().rstrip("….").strip()
        tool_list = [t.strip() for t in raw_names.split("+") if t.strip()]
        if len(tool_list) > 6:
            tool_display = " + ".join(tool_list[:6]) + f" 等 {len(tool_list)} 个"
        else:
            tool_display = " + ".join(tool_list)
        rows.append(f"<b>调用工具</b>：{html.escape(tool_display)}")
    if total_match:
        # total_match 的三个捕获组均来自 \d+ / [0-9.]+ 模式，天然只含数字
        # 和小数点，不存在需要转义的 HTML 元字符，故不再经
        # convert_markdown_to_telegram_html（对纯数字字符串它恒为直通，
        # 调用它只是无意义的额外函数开销）。
        rounds_s = total_match.group(1)
        tool_calls_s = total_match.group(2)
        seconds_s = total_match.group(3)
        if phase == "done":
            label = "完成"
        elif phase == "terminal":
            label = "结束"
        else:
            label = "进度"
        if seconds_s:
            rows.append(
                f"<b>{label}</b>：{rounds_s} 轮 · "
                f"{tool_calls_s} 次工具调用 · "
                f"{seconds_s}s"
            )
        else:
            rows.append(
                f"<b>{label}</b>：{rounds_s} 轮 · "
                f"{tool_calls_s} 次工具调用"
            )
    elif elapsed_match:
        rows.append(f"<b>已耗时</b>：{elapsed_match.group(1)}s")

    header = f"<p>{icon} <b>{phase_label}</b></p>"
    if rows:
        body = "<p>" + " · ".join(rows) + "</p>"
    else:
        # 兜底：状态文本本身已结构化失败，原样展示但截断到合理长度。
        # 这是全部分支里风险最高的一条——text 完全未经结构化拆解，可能
        # 包含任意字符，必须用 html.escape 而非
        # convert_markdown_to_telegram_html（原因同上：无 Markdown 语法
        # 时后者直接原样返回，不做转义）。
        safe = html.escape(text[:160])
        body = f"<p><i>{safe}</i></p>"
    return header + body


__all__ = ["subagent_progress_phase", "format_subagent_progress_html"]
