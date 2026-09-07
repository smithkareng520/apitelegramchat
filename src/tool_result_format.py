"""format_tool_result：工具原始结果 → (summary, details_html) UI 分发（自 tool_executors.py 拆出）。"""

import json
import re
import html
from typing import List

from tool_dispatch import _TOOL_TIMEOUT_MARKER

from utils import escape_html
from todo_tool import render_todo_card
from memory_tool import render_memory_card
from subagent_tool import render_subagent_card
from ai.web_search_render import format_web_search_result as _format_web_search_result
from tool_ui_render import (
    _extract_bash_command_from_envelope,
    _format_image_generation_result,
    _parse_bash_envelope,
    _render_bash_result,
    _render_code_panel,
    _render_code_text,
    _render_editor_result,
    _render_media_failure_result,
    _render_structured_payload,
    extract_domain,
)

import logging

logger = logging.getLogger(__name__)


# ---------- 工具结果格式化 ----------

# Magic marker emitted by ai_handlers.run_one on asyncio.TimeoutError.
# format_tool_result intercepts this BEFORE any other branch so we can
# surface a user-safe message and avoid leaking the actual timeout value.
# Human-readable label per tool name, used when surfacing timeout messages.
# Falls back to the raw fn_name if not listed here.
_TOOL_TIMEOUT_LABELS = {
    "web_search": "Web search",
    "fetch_url": "Page fetch",
    "wikipedia": "Wikipedia lookup",
    "exchange_rate": "Exchange rate lookup",
    "book_lookup": "Book lookup",
    "weather": "Weather fetch",
    "news": "News fetch",
    "crypto_price": "Crypto price lookup",
    "qr_code": "QR code generation",
    "generate_video": "Video generation",
    "geocode": "Geocoding",
    "route": "Route planning",
    "distance": "Distance calculation",
    "poi_keyword_search": "POI keyword search",
    "poi_nearby_search": "Nearby POI search",
    "poi_details": "POI detail lookup",
    "text_editor": "Text editor operation",
    "bash": "Bash command",
    "present_files": "File presentation",
}

# ---------- web_search 结果解析与渲染 ----------
# 实现拆到 ai.web_search_render，避免在 tool_executors
# 里维护大段正则与渲染函数；这里只暴露 _format_web_search_result 给
# format_tool_result 调用，保持调用点零改动。


async def format_tool_result(fn_name: str, fn_args: dict, result_str: str) -> tuple[str, str]:
    # 统一用 utils.escape_html 做转义（它做了智能 ampersand 处理，
    # 不会重复转义）；不要在本地另写简化版 escape_text——对已经合法的
    # 实体再做一次 `&` -> `&amp;` 转换会导致双重转义。
    # ---- Intercept timeout magic marker BEFORE any other branch ----
    # The raw exception (with TOOL_CALL_TIMEOUT seconds) is kept in
    # logger.error on the backend; the UI only sees the friendly version.
    if result_str == _TOOL_TIMEOUT_MARKER:
        label = _TOOL_TIMEOUT_LABELS.get(fn_name, fn_name)
        summary = f"⏱️ {label} timed out"
        timeout_message = "Execution exceeded the timeout limit. Please refine your request or try again later."
        if fn_name in {"generate_image_from_text", "edit_image_with_reference", "generate_video"}:
            details_html = _render_media_failure_result(timeout_message, timeout_message)
        else:
            details_html = timeout_message
        return summary, details_html

    if fn_name == "web_search":
        return _format_web_search_result(fn_args, result_str)

    elif fn_name == "fetch_url":
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
            else:
                m = re.search(r'🏷️\s+([^\n]+)', text)
                if m:
                    title = m.group(1).strip()
            summary = f"🌐 Fetched: {title}"
            details_html = f"{title} <a href=\"{url}\">{domain}</a>"
        return summary, details_html

    elif fn_name == "weather":
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
                details_html += "<details><summary>🌙 天文 &amp; 其他概率</summary><br/>"
                for day in daily[:5]:
                    date = day.get("date", "")
                    moon_phase = day.get("moon_phase", "--")
                    moon_illum = day.get("moon_illumination", "0") + "%"
                    snow = day.get("chance_of_snow", "0") + "%"
                    thunder = day.get("chance_of_thunder", "0") + "%"
                    fog = day.get("chance_of_fog", "0") + "%"
                    frost = day.get("chance_of_frost", "0") + "%"
                    details_html += f"<b>{date}</b>：月相 {moon_phase}（{moon_illum}），雪 {snow}，雷暴 {thunder}，雾 {fog}，霜冻 {frost}<br/>"
                details_html += "</details><br/>"
                details_html += "</details><br/>"

            if hourly:
                details_html += "<details><summary>⏰ 逐时预报（展开）</summary><br/>"
                details_html += "<table bordered striped cellpadding='3'>"
                details_html += "<tr><th>时间</th><th>天气</th><th>温度</th><th>降水</th><th>湿度</th><th>风速</th><th>气压</th><th>UV</th></tr>"
                for h in hourly[:24]:
                    time_str = h.get("time", "")
                    cond_h = h.get("condition", "")
                    temp_h = h.get("temp", "N/A")
                    precip_h = h.get("precip", "0")
                    humidity_h = h.get("humidity", "N/A")
                    wind_speed_h = h.get("wind_speed", "N/A")
                    pressure_h = h.get("pressure", "N/A")
                    uv_h = h.get("uvIndex", "N/A")
                    details_html += f"<tr><td>{time_str}</td><td>{cond_h}</td><td align='right'>{temp_h}{unit_display}</td><td align='right'>{precip_h} mm</td><td align='right'>{humidity_h}%</td><td align='right'>{wind_speed_h} km/h</td><td align='right'>{pressure_h} mb</td><td align='center'>{uv_h}</td></tr>"
                details_html += "</table><br/>"
                details_html += "<details><summary>🌪️ 逐时额外数据（阵风、云量、能见度、风向、概率、露点等）</summary><br/>"
                details_html += "<table bordered striped cellpadding='2'>"
                details_html += "<tr><th>时间</th><th>阵风</th><th>云量</th><th>能见度</th><th>风向</th><th>雨%</th><th>雪%</th><th>雷暴%</th><th>雾%</th><th>霜冻%</th><th>露点</th><th>热指数</th><th>风寒</th></tr>"
                for h in hourly[:24]:
                    time_str = h.get("time", "")
                    gust = h.get("wind_gust", "N/A")
                    cloud = h.get("cloudcover", "N/A")
                    vis = h.get("visibility", "N/A")
                    wind_dir = h.get("wind_dir", "N/A")
                    rain = h.get("chance_of_rain", "0") + "%"
                    snow = h.get("chance_of_snow", "0") + "%"
                    thunder = h.get("chance_of_thunder", "0") + "%"
                    fog = h.get("chance_of_fog", "0") + "%"
                    frost = h.get("chance_of_frost", "0") + "%"
                    dew = h.get("DewPointC", "N/A")
                    heat = h.get("HeatIndexC", "N/A")
                    chill = h.get("WindChillC", "N/A")
                    details_html += f"<tr><td>{time_str}</td><td align='right'>{gust} km/h</td><td align='right'>{cloud}%</td><td align='right'>{vis} km</td><td>{wind_dir}</td><td align='right'>{rain}</td><td align='right'>{snow}</td><td align='right'>{thunder}</td><td align='right'>{fog}</td><td align='right'>{frost}</td><td align='right'>{dew}°C</td><td align='right'>{heat}°C</td><td align='right'>{chill}°C</td></tr>"
                details_html += "</table>"
                details_html += "</details><br/>"
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

    elif fn_name == "wikipedia":
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
        summary = f"📚 {escape_html(title)}"
        details_html = f'<a href="{wiki_url}">{escape_html(title)}</a>'
        return summary, details_html

    elif fn_name == "exchange_rate":
        base = fn_args.get('base', 'USD')
        summary = f"💱 {escape_html(base)} 汇率"
        # result_str 可能是成功 HTML，也可能是以 "失败：" 开头的错误文本。
        # 后者含上游错误消息，需要 escape 以免打坏 Telegram 渲染。
        details_html = result_str if not result_str.startswith("失败：") else escape_html(result_str)
        return summary, details_html

    elif fn_name == "book_lookup":
        query = fn_args.get('query', '')
        summary = f"📖 {escape_html(query)}"
        details_html = result_str if not result_str.startswith("失败：") else escape_html(result_str)
        return summary, details_html

    elif fn_name == "news":
        source = fn_args.get('source', 'news')
        summary = f"📰 {escape_html(source.upper())} 新闻"
        details_html = result_str if not result_str.startswith("失败：") else escape_html(result_str)
        return summary, details_html

    elif fn_name == "crypto_price":
        coin = fn_args.get('coin', '')
        summary = f"💰 {escape_html(coin.upper())} 价格"
        details_html = result_str if not result_str.startswith("失败：") else escape_html(result_str)
        return summary, details_html

    elif fn_name == "qr_code":
        if "✅ 二维码生成成功" in result_str:
            img_match = re.search(r'图片链接：([^\s]+)', result_str)
            content_match = re.search(r'内容：([^\n]+)', result_str)
            if img_match:
                img_url = img_match.group(1)
                content_text = content_match.group(1) if content_match else "已编码内容"
                summary = "📱 二维码已生成"
                details_html = (
                    f'<img src="{img_url}"/><br/>'
                    f'<b>✅ 二维码生成成功</b><br/>'
                    f'<b>内容：</b>{escape_html(content_text)}<br/>'
                    f'<b>链接：</b><a href="{img_url}">📷 点击查看 / 下载二维码</a>'
                )
                return summary, details_html
        summary = "📱 二维码"
        details_html = escape_html(result_str)
        return summary, details_html

    elif fn_name == "generate_image_from_text":
        return _format_image_generation_result(
            result_str,
            operation_en="Generated",
            operation_zh="已生成",
            failure_summary="🎨 图片生成失败",
            failure_fallback="图片生成未完成，请稍后重试。",
        )

    elif fn_name == "edit_image_with_reference":
        return _format_image_generation_result(
            result_str,
            operation_en="Edited",
            operation_zh="已编辑",
            failure_summary="🎨 图片编辑失败",
            failure_fallback="图片编辑未完成，请稍后重试。",
        )

    elif fn_name == "generate_video":
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
                # 必须用 escape_html 转义（与 _agentic_loop_native_video 路径一致）。
                # 修复：旧实现注释声称已转义但 f-string 直接内插原始 URL，转义
                # 实际从未发生；现在真正落到 html.escape（含引号，供属性值使用）。
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

    # ===================== 地图工具（amap-maps MCP 直通） =====================
    # 所有地理 / 路径 / POI / 距离 / IP 工具都委托给 amap-maps MCP 服务，
    # 返回内容是该 MCP 的原生输出（通常是 JSON 文本）。这里不解析特定
    # schema，改为：
    #   - 若解析出 JSON 且含 status=error，则显示为失败
    #   - 否则把原始输出转义后直接展示给用户，让 LLM 在后续轮次里自由解读。
    elif fn_name in ("geocode", "route", "distance", "poi_keyword_search",
                     "poi_nearby_search", "poi_details"):
        label_map = {
            "geocode":            "📍 地理编码",
            "route":              "🚗 路线规划",
            "distance":           "📏 距离测量",
            "poi_keyword_search": "📍 POI 关键词搜索",
            "poi_nearby_search": "📍 POI 周边搜索",
            "poi_details":        "📍 POI 详情",
        }
        base_label = label_map.get(fn_name, fn_name)

        # 尝试 JSON 解析；只用于识别明确的 error 状态。
        try:
            data = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            data = None

        if isinstance(data, dict) and data.get("status") == "error":
            message = data.get("message") or result_str
            summary = f"❌ {base_label}失败"
            details_html = escape_html(str(message))
            return summary, details_html

        summary = base_label
        # 外部 MCP 常返回 JSON 文本。对聊天界面渲染结构化卡片，而向模型仍保留原始结果。
        details_html = _render_structured_payload(result_str, map_tool=fn_name) or _render_code_panel("服务响应 · 最近 10 行", result_str)
        return summary, details_html

    elif fn_name == "text_editor":
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

    # ===================== Todo 工具格式化 =====================
    # execute_todo 返回 JSON 字符串（给 AI 阅读）。UI 这里把它渲染成富文本卡片：
    #   - 顶部统计：总数 / 已完成 / 待办
    #   - 列表项：状态 emoji + 优先级徽章 + 标题（完成则加删除线）+ 标签 chips
    #   - 长列表自动截断并提示
    # 这里仅渲染工具调用气泡里的折叠预览。
    elif fn_name == "todo":
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "📋 待办操作"
            details_html = escape_html(result_str)
            return summary, details_html

        if not payload.get("ok"):
            summary = f"❌ 待办操作失败：{payload.get('code', '')}"
            details_html = f"<p>{escape_html(payload.get('error', '未知错误'))}</p>"
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
    # execute_memory 返回 JSON 字符串（给 AI 阅读），这里渲染成富文本卡片。
    elif fn_name == "memory":
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "🧠 记忆操作"
            details_html = escape_html(result_str)
            return summary, details_html
        if not payload.get("ok"):
            summary = f"❌ 记忆操作失败：{payload.get('code', '')}"
            details_html = f"<p>{escape_html(payload.get('error', '未知错误'))}</p>"
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
    # execute_subagent 返回 JSON，含 answer / rounds / tool_calls / elapsed。
    # 父 agent 在工具气泡里看到完整子 agent 答复；用户也能从气泡折叠区阅读。
    elif fn_name == "subagent":
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            summary = "🤖 子 agent"
            details_html = escape_html(result_str)
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
    elif fn_name == "bash":
        # 优先展示模型提供的意图描述（_description/_summary），让用户一眼
        # 看到命令目的；未提供时退化为命令首行摘要。意图文本直接原样展示、
        # 不加符号，与进行时摘要（tool_summary._generate_initial_tool_summary、
        # rich_message_builder._refresh_outer_summary 的 custom_desc 规范）一致，
        # 保证执行中与完成后摘要一致、不闪烁变化。
        # 延迟导入：tool_summary 模块级导入了 tool_executors，顶层导入会循环。
        from ai.tool_summary import _get_tool_description_from_args
        intent = _get_tool_description_from_args(fn_args) or ""
        # 用信封里的退出码判定失败：对输出内容做 "Error:" 子串匹配会把
        # `grep Error: app.log` 这类命令（exit 0）误标为执行失败。
        parsed_env = _parse_bash_envelope(result_str)
        bash_failed = (
            (parsed_env is not None and parsed_env[1] not in ("", "0"))
            or (parsed_env is None and ("Error:" in result_str or "Command rejected" in result_str))
        )
        if bash_failed:
            summary = "❌ Bash 执行失败"
        elif intent:
            summary = intent
        else:
            # 优先从工具调用参数取原始命令：多行命令从结果信封逐行解析
            # 只能拿到第一行，摘要会退化成 `python3 -c "` 这样的残句。
            args_command = ""
            if isinstance(fn_args, dict):
                raw = fn_args.get("command")
                if isinstance(raw, str):
                    args_command = raw
            cmd_line = args_command.strip() or _extract_bash_command_from_envelope(result_str)
            cmd_line = cmd_line.splitlines()[0].strip() if cmd_line else ""
            if len(cmd_line) > 30:
                cmd_line = cmd_line[:30] + "…"
            summary = f"🖥 {cmd_line or '命令已完成'}"
        # 保留命令元信息（Input 只展示原始命令，意图由上方摘要行单独呈现；
        # Output 保头保尾），避免长输出撑爆工具卡片的同时让用户始终能看到结尾的报错。
        details_html = _render_bash_result(result_str, fn_args=fn_args)
        return summary, details_html

    elif fn_name == "present_files":
        # ---- Decoupled data abstraction ----
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
            # Legacy fallback: result_str was not JSON (e.g. an error string
            # from dispatch_tool_call's top-level exception handler). Render
            # it as escaped plain text so we never break the UI.
            summary = "📂 Presenting files"
            details_html = escape_html(result_str) or "<i>No files were processed.</i>"
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
            items = "".join(f"<li>{escape_html(str(f))}</li>" for f in sent)
            label = "file" if sent_count == 1 else "files"
            details_parts.append(f"<b>✅ Sent ({sent_count} {label})</b><ul>{items}</ul>")
        if failed:
            items = "".join(f"<li>{escape_html(str(f))}</li>" for f in failed)
            label = "file" if failed_count == 1 else "files"
            details_parts.append(f"<b>❌ Failed ({failed_count} {label})</b><ul>{items}</ul>")
        if error:
            details_parts.append(f"<i>{escape_html(str(error))}</i>")

        if not details_parts:
            details_parts.append("<i>No files were processed.</i>")

        details_html = "<br/>".join(details_parts)
        return summary, details_html
    else:
        summary = f"🔧 {fn_name}"
        details_html = escape_html(result_str)
        return summary, details_html
