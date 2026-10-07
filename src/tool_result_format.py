"""format_tool_result：工具原始结果 → (summary, details_html) UI 分发。

MCP 化重构后的分发口径
----------------------
工具名先经 tool_names.tool_family() 归一到「工具族」再分发，渲染器只感知
族名（web_search / weather / maps_* / todo / memory ...），
不感知 mcp__<server>__<tool> 的完整名 —— 外部/内部 MCP 工具与 host 内建
工具共用同一套卡片渲染器。

结果策略（只给用户有帮助的）：
  - 有专属卡片的工具（POI 卡 / 路线卡 / 天气卡 / todo 卡 / 子 agent 卡 ...）
    渲染结构化富文本，突出用户决策字段；
  - 未知 MCP 工具走通用结构化面板（裁剪 + 转义），绝不把上游原文整篇刷屏；
  - 敏感/超时路径只展示友好状态，不泄露内部细节。
"""

import html
import json
import logging
import re

from core.text_utils import extract_domain
from typing import List

from tool_dispatch import _TOOL_TIMEOUT_MARKER
from tool_names import tool_family

from markdown_converter import render_telegram_fragment as convert_markdown_to_telegram_html, _escape_prose
from todo_tool import render_todo_card
from memory_tool import render_memory_card
from subagent_tool import render_subagent_card
from tool_ui_render import (
    _format_image_generation_result,
    _render_bash_result,
    _render_code_panel,
    _render_code_text,
    _render_editor_quote,
    _render_editor_result,
    _render_media_failure_result,
    _render_structured_payload,
)

logger = logging.getLogger(__name__)


# ---------- 工具结果格式化 ----------

# Magic marker emitted by ai_handlers.run_one on asyncio.TimeoutError.
# format_tool_result intercepts this BEFORE any other branch so we can
# surface a user-safe message and avoid leaking the actual timeout value.
# Human-readable label per tool family, used when surfacing timeout messages.
# Falls back to the raw fn_name if not listed here.
_TOOL_TIMEOUT_LABELS = {
    "web_search": "Web search",
    "fetch_url": "Page fetch",
    "wikipedia": "Wikipedia lookup",
    "exchange_rate": "Exchange rate lookup",
    "weather": "Weather fetch",
    "generate_video": "Video generation",
    "maps_geo": "Geocoding",
    "maps_regeocode": "Reverse geocoding",
    "maps_direction_driving": "Driving route planning",
    "maps_direction_walking": "Walking route planning",
    "maps_direction_bicycling": "Cycling route planning",
    "maps_direction_transit_integrated": "Transit route planning",
    "maps_distance": "Distance calculation",
    "poi_list": "POI search",
    "poi_detail": "POI details",
    "text_editor": "File operation",
    "todo": "Todo update",
    "memory": "Memory update",
    "subagent": "Subagent",
    "bash": "Bash",
    "present_files": "File delivery",
    "message_user": "User interaction",
    "deliver_reply": "Reply delivery",
}


# 进行中卡片沿用时的友好名称（family → 展示名）。
_FAMILY_LABELS = {
    "maps_geo": "📍 地理编码",
    "maps_regeocode": "📍 逆地理编码",
    "maps_direction_driving": "🚗 驾车路线",
    "maps_direction_walking": "🚶 步行路线",
    "maps_direction_bicycling": "🚲 骑行路线",
    "maps_direction_transit_integrated": "🚌 公交路线",
    "maps_distance": "📏 距离测量",
    "poi_list": "📍 地点搜索",
    "poi_detail": "📍 地点详情",
    "wikipedia": "📚 Wikipedia",
    "exchange_rate": "💱 汇率查询",
}


def _is_background_task_call(fn_args: dict) -> bool:
    """按调用参数识别后台任务模式的 bash / subagent 调用：启动（run_in_background）
    或 task_action 查询/停止。task_id 单独出现不构成后台调用（防御形状）。"""
    if not isinstance(fn_args, dict):
        return False
    return bool(fn_args.get("run_in_background")) or bool(fn_args.get("task_action"))


def _format_background_task_result(fn_args: dict, result_str: str) -> tuple[str, str] | None:
    """后台任务结果的卡片渲染：summary = 结果首行（自带状态徽标），
    details = 全文的等宽引用块。非后台调用返回 None 交回通用分支。"""
    if not _is_background_task_call(fn_args):
        return None
    text = str(result_str or "").strip()
    if not text:
        return "后台任务（空结果）", _render_editor_quote("Output", "(empty)")
    first_line = text.splitlines()[0].strip()
    summary = first_line if len(first_line) <= 60 else first_line[:57] + "…"
    details_html = _render_editor_quote("Output", text)
    return summary, details_html


async def format_tool_result(fn_name: str, fn_args: dict, result_str: str) -> tuple[str, str]:
    """工具执行结果 →（折叠块标题, 展开详情 HTML）。"""
    fn_args = fn_args or {}
    family = tool_family(fn_name)

    # ---- 通用拦截：超时标记 ----
    if result_str == _TOOL_TIMEOUT_MARKER:
        label = _TOOL_TIMEOUT_LABELS.get(family, fn_name)
        summary = f"⏱️ {label} timed out"
        timeout_message = "Execution exceeded the timeout limit. Please refine your request or try again later."
        if family in {"generate_image", "generate_video"}:
            details_html = _render_media_failure_result(timeout_message, timeout_message)
        else:
            details_html = timeout_message
        return summary, details_html

    if family == "web_search":
        # 惰性导入：ai 包反向消费本模块（ai.tool_call_loop -> tool_executors
        # -> tool_result_format），模块级导入会形成 import 时刻的包间环。
        from ai.web_search_render import format_web_search_result
        return format_web_search_result(fn_args, result_str)

    elif family == "fetch_url":
        url = fn_args.get('url', '')
        domain = extract_domain(url)
        text = str(result_str or "")
        stripped = text.lstrip()
        # 新版 fetch_url 成功结果本身就是面向模型的 Telegram Rich HTML；正文
        # 文本里也可能出现"失败"字样，因此失败判断只看前缀，避免把谈论"失败"
        # 的新闻正文误判为抓取失败。
        if (stripped.startswith(("失败", "❌"))
                or stripped.lower().startswith(("error", "failed", "timeout", "exception"))
                or "超时" in stripped[:30]):
            logger.error(f"[fetch_url] Failed to fetch {url}: {text[:500]}")
            summary = f"🌐 Failed to fetch {domain}"
            details_html = "Unable to retrieve content. Check the URL or try again later."
        else:
            # 展示保持历史样式：仅标题 + 来源域名链接。富 HTML 是给模型看的，
            # 不在 Telegram 工具折叠面板中渲染（避免长消息 + 重复内容）。
            title = domain
            m = re.search(r'<h3[^>]*>(.*?)</h3>', text, re.S | re.I)
            if m:
                # <h3> 内容是已转义的 HTML 文本（&amp; 等），原样嵌入合法。
                title = re.sub(r'<[^>]+>', '', m.group(1)).strip() or domain
            summary = f"🌐 Fetched: {title}"
            details_html = f"{title} <a href=\"{url}\">{domain}</a>"
        return summary, details_html

    elif family == "weather":
        try:
            weather_data = json.loads(result_str)
            if "error" in weather_data:
                error_msg = weather_data["error"]
                summary = "🌤️ 天气查询失败"
                # 上游错误文本必须转义：未转义时其中的 < > & 会打坏
                # Rich Message 结构（旧实现直接内插，属注入面）。
                # 走 _render_code_text 复用总量兑底，错误信息也可能很长。
                details_html = _render_code_text(str(error_msg))
                return summary, details_html

            city = weather_data.get("city", "未知")
            current = weather_data.get("current", {})
            hourly = weather_data.get("hourly", [])
            daily = weather_data.get("daily", [])
            unit_display = "℃" if weather_data.get("unit") == "C" else "℉"

            temp = current.get("temp", "N/A")
            cond = current.get("condition", "")
            summary = f"🌤️ {city} {temp}{unit_display} {cond}"

            details_html = f"<b>{city} 详细天气</b><br/><br/>"
            details_html += "<h3>📍 当前天气</h3>"
            details_html += f"🌡️ 温度：{temp}{unit_display}（体感 {current.get('feels_like', 'N/A')}{unit_display}）<br/>"
            details_html += f"💧 湿度：{current.get('humidity', 'N/A')}% 💨 风速：{current.get('wind', 'N/A')} km/h"
            if current.get('wind_gust', 'N/A') != 'N/A':
                details_html += f"（阵风 {current['wind_gust']} km/h）"
            details_html += "<br/>"
            details_html += f"☁️ 云量：{current.get('cloudcover', 'N/A')}% 🌡️ 气压：{current.get('pressure', 'N/A')} mb<br/>"
            details_html += f"👁️ 能见度：{current.get('visibility', 'N/A')} km ☀️ 紫外线指数：{current.get('uvIndex', 'N/A')}<br/>"
            details_html += f"🌧️ 降水：{current.get('precip', '0.0')} mm 🧭 风向：{current.get('wind_dir', 'N/A')} ({current.get('wind_deg', 'N/A')}°)<br/>"
            details_html += f"🕒 观测时间：{current.get('obs_time', '')}<br/>"
            details_html += f"🌥️ 天气状况：{cond}<br/><br/>"

            if daily:
                details_html += "<details><summary>📅 未来几天预报（展开）</summary><br/>"
                details_html += "<table bordered striped cellpadding='3'>"
                details_html += "<tr><th>日期</th><th>天气</th><th>最高</th><th>最低</th><th>UV</th><th>日出</th><th>日落</th><th>降水%</th></tr>"
                for day in daily[:5]:
                    date = day.get("date", "")
                    cond_d = day.get("condition", "")
                    max_t = day.get("max", "N/A")
                    min_t = day.get("min", "N/A")
                    max_display = f"{max_t}{unit_display}" if max_t != "N/A" else "--"
                    min_display = f"{min_t}{unit_display}" if min_t != "N/A" else "--"
                    uv = day.get("uvIndex", "N/A")
                    sunrise = day.get("sunrise", "--")
                    sunset = day.get("sunset", "--")
                    rain = day.get("chance_of_rain", "0") + "%"
                    details_html += f"<tr><td>{date}</td><td>{cond_d}</td><td align='right'>{max_display}</td><td align='right'>{min_display}</td><td align='center'>{uv}</td><td>{sunrise}</td><td>{sunset}</td><td align='right'>{rain}</td></tr>"
                details_html += "</table><br/>"
                details_html += "</details><br/>"

            if hourly:
                details_html += "<details><summary>⏰ 逐时预报（展开）</summary><br/>"
                details_html += "<table bordered striped cellpadding='3'>"
                details_html += "<tr><th>时间</th><th>天气</th><th>温度</th><th>降水</th><th>湿度</th><th>风速</th><th>降水概率</th></tr>"
                for h in hourly[:24]:
                    time_str = h.get("time", "")
                    cond_h = h.get("condition", "")
                    temp_h = h.get("temp", "N/A")
                    precip_h = h.get("precip", "0")
                    humidity_h = h.get("humidity", "N/A")
                    wind_speed_h = h.get("wind_speed", "N/A")
                    rain_h = h.get("chance_of_rain", "0") + "%"
                    details_html += f"<tr><td>{time_str}</td><td>{cond_h}</td><td align='right'>{temp_h}{unit_display}</td><td align='right'>{precip_h} mm</td><td align='right'>{humidity_h}%</td><td align='right'>{wind_speed_h} km/h</td><td align='right'>{rain_h}</td></tr>"
                details_html += "</table>"
                details_html += "</details><br/>"

            tips = []
            cond_lower = cond.lower()
            if "雨" in cond or "rain" in cond_lower:
                tips.append("🌂 今天有降水，出门记得带伞。")
            if "霾" in cond or "haze" in cond_lower or "烟雾" in cond:
                tips.append("😷 空气中有雾霾，建议佩戴口罩或减少户外活动。")
            try:
                if int(temp) > 30:
                    tips.append("☀️ 气温较高，注意防暑降温，多补充水分。")
            except (ValueError, TypeError):
                pass
            try:
                if int(current.get('uvIndex', 0)) >= 8:
                    tips.append("🧴 紫外线指数高，外出请做好防晒。")
            except (ValueError, TypeError):
                pass
            try:
                if int(current.get('visibility', 10)) < 2:
                    tips.append("🌫️ 能见度较低，驾车请减速慢行。")
            except (ValueError, TypeError):
                pass
            try:
                if int(current.get('wind', 0)) > 30:
                    tips.append("💨 风速较大，注意防风。")
            except (ValueError, TypeError):
                pass
            if "雪" in cond or "snow" in cond_lower:
                tips.append("❄️ 有降雪，路面湿滑，注意出行安全。")
            if tips:
                details_html += "<b>💡 温馨提示</b><br/>" + "<br/>".join(tips)

            return summary, details_html

        except json.JSONDecodeError:
            # 严格转义 + <pre> 总量兑底：_render_code_text 内部先裁剪后转义，
            # 避免单行 60KB 的错误响应把块与整条草稿撑爆，也不会切断实体。
            summary = "🌤️ 天气数据"
            details_html = _render_code_text(result_str[:60000])
            return summary, details_html

    elif family == "wikipedia":
        query = fn_args.get('query', '')
        lang = fn_args.get('lang', 'zh')
        import urllib.parse
        text = result_str.strip()
        # 标题：富 HTML 结果取首个 <h3>；退化（纯文本摘要）取 <b>Wikipedia — 标题</b>。
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
        # 来源链接：优先结果中的真实 URL——关键词解析出的页面标题
        # 可能与 query 不同（如搜"可塑性记忆"命中"可塑性記憶"），
        # 猜测 URL 会 404。富 HTML 里是 <a href>；退化格式里是纯文本。
        m = re.search(r'<a href="(https://[^"]*wikipedia\.org[^"]*)"', text)
        if m:
            wiki_url = m.group(1)
        else:
            m = re.search(r"https://[^\s<>\"']+wikipedia\.org[^\s<>\"']*", text)
            wiki_url = m.group(0) if m else f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(query)}"
        summary = f"📚 {convert_markdown_to_telegram_html(title)}"
        details_html = f'<a href="{wiki_url}">{convert_markdown_to_telegram_html(title)}</a>'
        return summary, details_html

    elif family == "exchange_rate":
        base = fn_args.get('base', 'USD')
        summary = f"💱 {convert_markdown_to_telegram_html(base)} 汇率"
        # 失败分支必须用 _escape_prose 无条件转义：错误文本通常不含
        # Markdown 语法，convert_markdown_to_telegram_html 会短路透传，
        # 上游错误消息里的 < > & 会原样打坏 Rich Message 结构。
        details_html = result_str if not result_str.startswith("失败：") else _escape_prose(result_str)
        return summary, details_html

    elif family == "message_user":
        mode = str(fn_args.get('mode') or '').strip().lower()
        question = str(fn_args.get('question', '') or '')
        message = str(fn_args.get('message', '') or '')
        questions = fn_args.get('questions')
        input_lines = []
        if mode == 'form' or isinstance(questions, list):
            for idx, item in enumerate(questions[:8] if isinstance(questions, list) else []):
                if not isinstance(item, dict):
                    continue
                q = str(item.get('question', '') or '').strip()
                if q:
                    input_lines.append(f"Q{idx + 1}: {q}")
                opts = item.get('options')
                if isinstance(opts, list):
                    labels = [str(o.get('label', '')).strip() for o in opts[:8] if isinstance(o, dict) and str(o.get('label', '')).strip()]
                    if labels:
                        input_lines.append("options: " + " | ".join(labels))
        else:
            text = message or question
            if text:
                input_lines.append(text)
        summary = "💬 Messaged you"
        details_html = ""
        if input_lines:
            details_html += _render_editor_quote("Input", "\n".join(input_lines))
        details_html += _render_editor_quote("Output", result_str)
        return summary, details_html

    elif family == "generate_image":
        # 按 image_url 是否携带判断本次是生成还是编辑，结果折叠块标题显示对应操作。
        is_edit = bool(str(fn_args.get("image_url") or "").strip())
        if is_edit:
            return _format_image_generation_result(
                result_str,
                operation_en="Edited",
                operation_zh="已编辑",
                failure_summary="🎨 图片编辑失败",
                failure_fallback="图片编辑未完成，请稍后重试。",
            )
        return _format_image_generation_result(
            result_str,
            operation_en="Generated",
            operation_zh="已生成",
            failure_summary="🎨 图片生成失败",
            failure_fallback="图片生成未完成，请稍后重试。",
        )

    elif family == "generate_video":
        # 视频通过 <figure><video> 内嵌在工具结果卡片里渲染（Telegram Rich Message
        # 支持视频 block 与文本同消息共存，参见 Rich Message Formatting Options）。
        # execute_generate_video 返回的结构：
        #   ✅ 已生成视频。
        #   视频链接：https://...
        if "✅" in result_str:
            url_match = re.search(r'视频链接：(https?://[^\s]+)', result_str)
            if url_match:
                # ⚠️ R2 presigned URL 含大量 & 查询参数（X-Amz-Algorithm、X-Amz-Credential、
                # X-Amz-Signature 等），HTML 属性值中未转义的 & 会被 Telegram HTML
                # 解析器当作实体名起点，导致 URL 被截断 → RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND。
                # 必须转义（与 _agentic_loop_native_video 路径一致）。此处特意用
                # html.escape 而非 convert_markdown_to_telegram_html：后者是
                # markdown 转换器，会把 URL 里的 *_[]` 等字符误解析成标签，同样
                # 会打坏这个 href/src 属性值。
                video_url = url_match.group(1).strip()
                duration_str = ""
                m = re.search(r'(\d+)\s*秒', fn_args.get("prompt", "") or "")
                if m:
                    duration_str = f" · {m.group(1)}s"
                summary = f"🎬 Video generated{duration_str}"
                video_url_attr = html.escape(video_url, quote=True)
                # <figure><video> 是一个独立 media block，可以与其他 block 同消息发送；
                # 附带简短文本链接 caption，避免裸 R2 presigned URL 刷屏
                details_html = (
                    f'<figure><video src="{video_url_attr}"></video>'
                    f'<figcaption><a href="{video_url_attr}">下载 / 查看视频</a></figcaption>'
                    f'</figure>'
                )
                return summary, details_html
        summary = "🎬 视频生成失败"
        details_html = _render_media_failure_result(result_str, "视频生成未完成，请稍后重试。")
        return summary, details_html

    # ===================== 高德地图工具（gaode_mcp 原生直连） =====================
    # 模型直接调用 mcp__gaode_mcp__maps_*；UI 按族渲染结构化卡片（POI 卡 /
    # 路线卡 / 距离表 / geocode 卡）。载荷是高德原生 JSON（未做模型视图清洗
    # 的完整版），POI 卡片因此可以展示 photos 实景图。
    elif family in ("maps_geo", "maps_regeocode", "maps_direction_bicycling", "maps_direction_walking", "maps_direction_driving", "maps_direction_transit_integrated", "maps_distance", "maps_text_search", "maps_around_search", "maps_search_detail"):
        base_label = _FAMILY_LABELS.get(family, f"📍 {fn_name}")

        # 尝试 JSON 解析；只用于识别明确的 error 状态。
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            data = None

        if isinstance(data, dict) and (
            data.get("status") == "error"
            or (str(data.get("status")) == "0" and not (family in ("maps_geo", "maps_regeocode") and data.get("geocodes")))
        ):
            message = data.get("message") or data.get("info") or result_str
            summary = f"❌ {base_label}失败"
            details_html = convert_markdown_to_telegram_html(str(message))
            return summary, details_html

        summary = base_label
        # 对聊天界面渲染结构化卡片，而向模型仍保留原始结果（经模型视图清洗）。
        details_html = _render_structured_payload(result_str, map_tool=family) or _render_code_panel("服务响应 · 最近 10 行", result_str)
        return summary, details_html

    elif family == "text_editor":
        command = fn_args.get("command", "")
        path = fn_args.get("path", "")
        # 工具自身错误总是以 "Error" 开头；view 返回的是文件内容，
        # 内容里出现 "Error:"（如查看日志文件）不代表操作失败。
        if (result_str or "").startswith("Error"):
            summary = "❌ 文件操作未完成"
        elif command == "view":
            summary = f"📄 查看 {path}" if path else "📄 查看文件"
        elif command == "create":
            summary = f"📄 已创建 {path}" if path else "📄 已创建文件"
        elif command in ("str_replace", "insert"):
            summary = f"📝 已更新 {path}" if path else "📝 已更新文件"
        elif command == "delete":
            summary = f"🗑️ 已删除 {path}" if path else "🗑️ 已删除文件"
        else:
            summary = "📝 文件操作"
        # 每个编辑结果都优先展示写入后文件的最后十行（含绝对行号）。
        details_html = _render_editor_result(command, path, result_str, fn_args)
        return summary, details_html

    # Todo 工具结果格式化
    # internal_todo MCP 服务器返回 JSON 字符串（给 AI 阅读）。UI 渲染富文本卡片：
    #   - 顶部统计：总数 / 已完成 / 待办
    #   - 列表项：状态 emoji + 优先级徽章 + 标题（完成则加删除线）+ 标签 chips
    #   - 长列表自动截断并提示
    elif family == "todo":
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "📋 待办操作"
            details_html = _render_editor_quote("Output", result_str)
            return summary, details_html

        if not payload.get("ok"):
            summary = f"❌ 待办操作失败：{payload.get('code', '')}"
            details_html = f"<p>{convert_markdown_to_telegram_html(payload.get('error', '未知错误'))}</p>"
            return summary, details_html

        action = payload.get("action", "list")
        if action == "list":
            total = payload.get("total", 0)
            pending = payload.get("pending", 0)
            summary = f"📋 共 {total} 项 · 待办 {pending} 项"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "add":
            t = payload.get("todo", {})
            summary = f"➕ 新增 {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action in ("done", "undone", "toggle"):
            t = payload.get("todo", {})
            icon = "✅" if t.get("done") else "↩️"
            summary = f"{icon} {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "delete":
            t = payload.get("todo", {})
            summary = f"🗑️ 删除 {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "clear":
            summary = f"🧹 清理 {payload.get('removed', 0)} 条"
            details_html = render_todo_card(payload)
            return summary, details_html
        if action == "edit":
            t = payload.get("todo", {})
            summary = f"📝 编辑 {t.get('title', '')[:30]}"
            details_html = render_todo_card(payload)
            return summary, details_html
        summary = "📋 待办操作"
        details_html = render_todo_card(payload)
        return summary, details_html

    # ===================== Memory 工具格式化 =====================
    elif family == "memory":
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "🧠 记忆操作"
            details_html = _render_editor_quote("Output", result_str)
            return summary, details_html
        if not payload.get("ok"):
            summary = f"❌ 记忆操作失败：{payload.get('code', '')}"
            details_html = f"<p>{convert_markdown_to_telegram_html(payload.get('error', '未知错误'))}</p>"
            return summary, details_html
        action = payload.get("action", "list")
        if action == "list":
            total = payload.get("total", 0)
            shown = payload.get("shown", 0)
            summary = f"🧠 记忆库：{total} 条 · 显示 {shown} 条"
        elif action == "search":
            summary = f"🔎 记忆搜索：{payload.get('matches', 0)} / {payload.get('total', 0)} 条命中"
        elif action == "add":
            m = payload.get("memory", {})
            summary = f"🧠 保存 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "get":
            m = payload.get("memory", {})
            summary = f"🧠 查看 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "update":
            m = payload.get("memory", {})
            summary = f"📝 更新 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "delete":
            m = payload.get("memory", {})
            summary = f"🗑️ 删除 #{m.get('id', '?')} {m.get('content', '')[:30]}"
        elif action == "clear":
            summary = f"🧹 清理 {payload.get('removed', 0)} 条记忆"
        else:
            summary = "🧠 记忆操作"
        details_html = render_memory_card(payload)
        return summary, details_html

    # ===================== Subagent 工具格式化 =====================
    elif family == "subagent":
        # 后台模式（启动句柄 / task_action 查询与停止）：结果是首行自带
        # 状态徽标的纯文本，与 bash 后台任务同一渲染。
        bg_rendered = _format_background_task_result(fn_args, result_str)
        if bg_rendered is not None:
            return bg_rendered
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "🤖 子 agent"
            details_html = _render_editor_quote("Output", result_str)
            return summary, details_html
        ok = payload.get("ok", False)
        model_name = payload.get("model_name") or payload.get("model") or "?"
        rounds = payload.get("rounds", 0)
        tool_calls = payload.get("tool_calls", 0)
        elapsed = payload.get("elapsed", 0)
        if ok:
            summary = f"🤖 {model_name} · {rounds} 轮 · {tool_calls} 工具 · {elapsed:.1f}s"
        else:
            err = payload.get("error", "未知错误")
            summary = f"❌ {model_name} 失败 · {rounds} 轮 · {err[:40]}"
        details_html = render_subagent_card(payload)
        return summary, details_html

    # ===================== Bash 工具格式化 =====================
    elif family == "bash":
        # 后台任务模式：启动、查询和停止结果的格式化。
        # 不走终端信封，直接以结果首行做 summary——bash_background 生成
        # 文本时首行已自带 emoji 徽标 + 任务标识，天然可作卡片摘要；
        # 全文放 Output 引用块。非后台调用返回 None，落回通用信封渲染。
        bg_rendered = _format_background_task_result(fn_args, result_str)
        if bg_rendered is not None:
            return bg_rendered
        # 优先展示模型提供的意图描述（description/_summary），让用户一眼
        # 看到命令目的；未提供时退化为命令首行摘要。意图文本直接原样展示、
        # 不加符号，与进行时摘要（tool_summary._generate_initial_tool_summary、
        # rich_message_builder._refresh_outer_summary 的 custom_desc 规范）一致，
        # 保证执行中与完成后摘要一致、不闪烁变化。
        # 延迟导入：tool_summary 模块级导入了 tool_executors，顶层导入会循环。
        from ai.tool_summary import _get_tool_description_from_args
        intent = _get_tool_description_from_args(fn_args) or ""
        # Bash 命令本身的非零退出码是命令结果，不是工具执行失败。
        # 工具级异常（超时、会话崩溃、命令被安全策略拒绝等）由统一
        # _tool_result_is_failure / timeout 路径负责判定。
        if intent:
            summary = intent
        else:
            # 只从工具调用参数取原始命令；结果已不再回显命令。
            args_command = ""
            if isinstance(fn_args, dict):
                raw = fn_args.get("command")
                if isinstance(raw, str):
                    args_command = raw
            cmd_line = args_command.strip()
            cmd_line = cmd_line.splitlines()[0].strip() if cmd_line else ""
            if len(cmd_line) > 30:
                cmd_line = cmd_line[:30] + "…"
            summary = f"🖥 {cmd_line or '命令已完成'}"
        # Input 来自本次工具调用参数，Output 只来自命令实际输出。
        details_html = _render_bash_result(result_str, fn_args=fn_args)
        return summary, details_html

    elif family == "present_files":
        # execute_present_files returns a JSON payload:
        #   {"sent": [...], "failed": [...]}   (+ "error": str only on early failure)
        # The model context receives this raw JSON (so it can reply concisely,
        # e.g. "Files sent"), while the UI gets a rich, detailed report built
        # from the parsed structure.
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            data = None

        if not isinstance(data, dict):
            # Fallback: result_str was not JSON (e.g. an error string
            # from dispatch_tool_call's top-level exception handler). Render
            # it as escaped plain text so we never break the UI.
            summary = "📂 Presenting files"
            details_html = _render_editor_quote("Output", result_str) or "<i>No files were processed.</i>"
            return summary, details_html

        sent = data.get("sent") or []
        failed = data.get("failed") or []
        error = data.get("error")
        # Be defensive: ensure both lists are actually lists.
        if not isinstance(sent, list):
            sent = []
        if not isinstance(failed, list):
            failed = []

        sent_count = len(sent)
        failed_count = len(failed)

        # ---- Summary with correct pluralization (guards None / 0) ----
        if sent_count == 0:
            summary = "📂 No files sent"
        elif sent_count == 1:
            summary = "📂 Presented 1 file"
        else:
            summary = f"📂 Presented {sent_count} files"

        # ---- Details: HTML list of successes and failures ----
        details_parts: List[str] = []
        if sent:
            items = "".join(f"<li>{convert_markdown_to_telegram_html(str(f))}</li>" for f in sent)
            label = "file" if sent_count == 1 else "files"
            details_parts.append(f"<b>✅ Sent ({sent_count} {label})</b><ul>{items}</ul>")
        if failed:
            items = "".join(f"<li>{convert_markdown_to_telegram_html(str(f))}</li>" for f in failed)
            label = "file" if failed_count == 1 else "files"
            details_parts.append(f"<b>❌ Failed ({failed_count} {label})</b><ul>{items}</ul>")
        if error:
            details_parts.append(f"<i>{convert_markdown_to_telegram_html(str(error))}</i>")

        if not details_parts:
            details_parts.append("<i>No files were processed.</i>")

        details_html = "<br/>".join(details_parts)
        return summary, details_html

    elif family == "deliver_reply":
        # deliver_reply 的终态摘要由 _generate_tool_summary_done 按实际
        # 结果生成（Delivered / Skipped the final reply）；这里的
        # formatted_summary 只在失败路径（"失败："前缀 → status=error）
        # 被采用，给出与 text_editor「❌ 文件操作未完成」同风格的标题。
        text = str(result_str or "")
        if text.startswith("未发送"):
            # send=false / TIMER 回合缺省 false：静默是正常终态，
            # 展示摘要走完成态分支，此处仅渲染 Output 面板备用。
            summary = "💬 已跳过交付"
        else:
            summary = "❌ 最终回复未交付"
        details_html = _render_editor_quote("Output", text)
        return summary, details_html
    else:
        # 未知工具（含未识别的 MCP 工具）的通用分支：优先尝试结构化面板，
        # 让 JSON 载荷以可阅读字段呈现；否则渲染为等宽代码面板，与
        # bash / text_editor 的卡片形态保持一致，同时避免上游文本中的
        # < > & 打坏 Rich Message 结构。
        summary = f"🔧 {fn_name}"
        details_html = _render_editor_quote("Output", result_str)
        return summary, details_html
