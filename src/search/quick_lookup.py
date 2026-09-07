"""生活查询工具：wikipedia / exchange_rate / book_lookup / weather / news / crypto / qr_code（自 search_engine.py 拆出）。"""

import asyncio
import hashlib
import json
import re
from io import BytesIO
from urllib.parse import quote
from typing import Any

import aiohttp
try:
    from curl_cffi.requests import AsyncSession
except Exception:  # pragma: no cover - optional dependency fallback
    AsyncSession = None  # type: ignore
try:
    import feedparser
except Exception:  # pragma: no cover - optional dependency fallback
    class _FeedParserStub:
        def parse(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"entries": []}
    feedparser = _FeedParserStub()
try:
    import qrcode
except Exception:  # pragma: no cover - optional dependency fallback
    qrcode = None

from utils import escape_html
from s3_utils import upload_bytes_to_r2
from search.fetch_url import CURL_TIMEOUT, HTTP_TIMEOUT_SHORT, _truncate

import logging

logger = logging.getLogger(__name__)


# --------------------- wikipedia ---------------------
async def execute_wikipedia(query: str, lang: str = "zh") -> str:
    """Wikipedia 关键词查询 → 忠实原文结构的 Telegram Rich HTML。

    链路：
      1. list=search 把关键词解析为最匹配的页面（web_search+fetch_url 需要
         两轮才能做到，且不保证维基百科排第一）；
      2. action=parse 获取该页面的完整解析后 HTML（MediaWiki API 并非只有
         纯文本：prop=extracts&explaintext 才是纯文本摘要；action=parse 的
         prop=text 返回含表格/列表/图片的完整 HTML，比抓取网页更稳定）；
      3. 复用 fetch_url 的富提取管线（trafilatura 结构化提取 + 媒体原位 +
         预算感知压缩），结果格式与 fetch_url 完全一致，模型可同样复用其中
         的 <img>/<a> 等片段；
      4. parse 失败或富转换提不出内容时，退化为旧的纯文本摘要路径。
    """
    try:
        from fetch_rich_content import build_model_facing_html
    except Exception as e:
        logger.error(f"[wikipedia] fetch_rich_content 导入失败: {e}")
        build_model_facing_html = None  # type: ignore[assignment]

    for l in [lang, "en"]:
        try:
            async with AsyncSession() as session:
                search_resp = await session.get(
                    f"https://{l}.wikipedia.org/w/api.php",
                    params={"action": "query", "list": "search", "srsearch": query, "srlimit": 3, "format": "json", "utf8": 1},
                    headers={"Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                    impersonate="chrome120", timeout=CURL_TIMEOUT
                )
                if search_resp.status_code != 200:
                    continue
                search_data = search_resp.json()
                results = search_data.get("query", {}).get("search", [])
                if not results:
                    continue
                page_id = results[0]["pageid"]

                # ---- 主路径：action=parse 完整 HTML → 富管线 ----
                if build_model_facing_html is not None:
                    try:
                        parse_resp = await session.get(
                            f"https://{l}.wikipedia.org/w/api.php",
                            params={
                                "action": "parse", "pageid": page_id, "prop": "text|displaytitle",
                                "redirects": 1, "disablelimitreport": 1, "disableeditsection": 1,
                                "disabletoc": 1, "format": "json", "utf8": 1,
                            },
                            headers={"Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                            impersonate="chrome120", timeout=CURL_TIMEOUT
                        )
                        if parse_resp.status_code == 200:
                            parse_data = (parse_resp.json() or {}).get("parse", {}) or {}
                            page_html = ((parse_data.get("text") or {}).get("*") or "").strip()
                            title = (parse_data.get("title") or results[0].get("title") or query).strip()
                            if page_html:
                                page_url = f"https://{l}.wikipedia.org/wiki/{quote(title)}"
                                # CPU 密集转换放到线程池，不阻塞事件循环
                                # （与 _build_rich_fetch_payload 同一调度方式）。
                                rich = await asyncio.to_thread(
                                    build_model_facing_html, page_url, page_html, None, title
                                )
                                if rich:
                                    return rich
                    except Exception as e:
                        logger.debug(f"[wikipedia] 富 HTML 路径失败（回退纯文本摘要）: {e}")

                # ---- 退化路径：纯文本摘要（历史行为）----
                page_resp = await session.get(
                    f"https://{l}.wikipedia.org/w/api.php",
                    params={"action": "query", "pageids": page_id, "prop": "extracts|info", "explaintext": True, "inprop": "url", "format": "json", "utf8": 1},
                    headers={"Accept": "application/json", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                    impersonate="chrome120", timeout=CURL_TIMEOUT
                )
                if page_resp.status_code != 200:
                    continue
                page_data = page_resp.json()
                pages = page_data.get("query", {}).get("pages", {})
                page: dict[str, Any] = next(iter(pages.values()), {})
                title = page.get("title", results[0].get("title", query))
                extract = page.get("extract", "").strip()
                if not extract:
                    continue
                extract = _truncate(extract)
                page_url = page.get("fullurl", f"https://{l}.wikipedia.org/wiki/{quote(title)}")
                return f"<b>Wikipedia — {title}</b><br/><br/>{extract}<br/><br/>链接：{page_url}"
        except Exception as exc:
            logger.warning("wikipedia 语言分支查询失败 lang=%s: %s", l, exc)
            continue
    return f"失败：Wikipedia 查询「{query}」未找到结果。"


# --------------------- exchange_rate ---------------------
async def execute_exchange_rate(base: str, target: str | None = None) -> str:
    base = base.upper().strip()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://open.er-api.com/v6/latest/{base}", timeout=HTTP_TIMEOUT_SHORT) as resp:
                if resp.status != 200:
                    return f"失败：汇率查询失败（HTTP {resp.status}）"
                data = await resp.json()
        if data.get("result") != "success":
            return f"失败：汇率查询失败：{data.get('error-type', '未知错误')}"
        rates = data.get("rates", {})
        update_time = data.get("time_last_update_utc", "未知")
        if target:
            target = target.upper().strip()
            if target not in rates:
                return f"失败：不支持的目标货币代码：{target}"
            return f"<b>汇率查询成功</b><br/>1 {base} = {rates[target]} {target}<br/>更新时间：{update_time}"
        major = ["CNY", "USD", "EUR", "JPY", "GBP", "HKD", "KRW", "SGD", "AUD", "CAD"]
        lines = [f"<b>{base} 汇率</b><br/>更新时间：{update_time}<br/>"]
        for cur in major:
            if cur in rates and cur != base:
                # 强制 float 转换：上游 API 偶尔返回字符串（如 "0.1234"），
                # 直接 :.4f 会抛 ValueError 被 outer except 吞成"汇率查询出错"。
                try:
                    rate_val = float(rates[cur])
                except (TypeError, ValueError):
                    continue
                lines.append(f"1 {base} = {rate_val:.4f} {cur}")
        return "<br/>".join(lines)
    except Exception as e:
        logger.debug("execute_exchange_rate 内部忽略的异常", exc_info=True)
        return f"失败：汇率查询出错：{str(e)[:100]}"


# --------------------- book_lookup ---------------------
async def execute_book_lookup(query: str) -> str:
    headers = {"User-Agent": "TelegramAIAssistant/1.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://openlibrary.org/search.json", params={"q": query, "limit": 5, "fields": "*"}, headers=headers, timeout=HTTP_TIMEOUT_SHORT) as resp:
                if resp.status != 200:
                    return f"失败：书籍查询失败（HTTP {resp.status}）"
                data = await resp.json()
        docs = data.get("docs", [])
        if not docs:
            return f"失败：未找到与「{query}」相关的书籍"
        lines = [f"<b>书籍查询结果：「{escape_html(query)}」</b><br/>"]
        for i, doc in enumerate(docs[:5], 1):
            title = escape_html(doc.get("title", "无标题"))
            authors = escape_html("、".join(doc.get("author_name", ["未知作者"])[:3]))
            year = escape_html(str(doc.get("first_publish_year", "未知")))
            subjects = escape_html("、".join(doc.get("subject", [])[:3]))
            key = doc.get("key", "")
            ol_url = f"https://openlibrary.org{key}" if key else ""
            ol_url_html = escape_html(ol_url) if ol_url else ""
            lines.append(f"{i}. 《{title}》<br/>   作者：{authors}<br/>   首次出版：{year} 年<br/>" + (f"   主题：{subjects}<br/>" if subjects else "") + (f"   详情：{ol_url_html}<br/>" if ol_url_html else ""))
        return "<br/>".join(lines)
    except Exception as e:
        logger.debug("execute_book_lookup 内部忽略的异常", exc_info=True)
        return f"失败：书籍查询出错：{str(e)[:100]}"


# --------------------- weather ---------------------
async def execute_weather(city: str, unit: str = "c", hours: int = 6) -> str:
    """查询 wttr.in 天气并打包为 JSON。

    注意：本函数返回的是「完整数据」（UI 折叠面板的月相/露点等展示依赖它）。
    hours 参数不在这一层生效 —— 发给模型的逐时条数与字段白名单由
    tool_result_condense.condense_for_model 的 weather 视图控制
    （默认 6 条，与工具 schema 的 hours 参数一致）。
    """
    url = f"https://wttr.in/{city}?format=j1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=HTTP_TIMEOUT_SHORT) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    return json.dumps({"error": f"无法解析天气数据：{text[:200]}"}, ensure_ascii=False)

                if resp.status != 200:
                    error_msg = data.get("error", {}).get("message", text)
                    return json.dumps({"error": f"天气查询失败（HTTP {resp.status}）：{error_msg[:200]}"}, ensure_ascii=False)

                current = (data.get("current_condition") or [{}])[0]
                current_data = {
                    "temp": current.get(f"temp_{unit.upper()}", "N/A"),
                    "feels_like": current.get(f"FeelsLike{unit.upper()}", "N/A"),
                    "humidity": current.get("humidity", "N/A"),
                    "wind": current.get("windspeedKmph", "N/A"),
                    "wind_gust": current.get("windgustKmph", "N/A"),
                    "pressure": current.get("pressure", "N/A"),
                    "visibility": current.get("visibility", "N/A"),
                    "cloudcover": current.get("cloudcover", "N/A"),
                    "uvIndex": current.get("uvIndex", "N/A"),
                    "precip": current.get("precipMM", "0.0"),
                    "wind_dir": current.get("winddir16Point", "N/A"),
                    "wind_deg": current.get("winddirDegree", "N/A"),
                    "condition": current.get("weatherDesc", [{}])[0].get("value", "未知"),
                    "weather_code": current.get("weatherCode", "N/A"),
                    "obs_time": current.get("localObsDateTime") or current.get("observation_time", ""),
                }

                first_day = (data.get("weather") or [{}])[0]
                hourly_list = first_day.get("hourly", [])
                hourly_data = []
                for h in hourly_list[:24]:
                    # wttr.in 的 time 字段是 "0"…"2300"（HHMM）。
                    time_str = h.get("time", "0")
                    try:
                        val = int(time_str)
                        time_label = f"{val // 100:02d}:00" if 0 <= val <= 2359 else str(time_str)
                    except (ValueError, TypeError):
                        time_label = str(time_str)

                    temp_key = f"temp{unit.upper()}"
                    hourly_data.append({
                        "time": time_label,
                        "temp": h.get(temp_key, "N/A"),
                        "condition": h.get("weatherDesc", [{}])[0].get("value", ""),
                        "precip": h.get("precipMM", "0"),
                        "humidity": h.get("humidity", "N/A"),
                        "pressure": h.get("pressure", "N/A"),
                        "wind_gust": h.get("WindGustKmph", "N/A"),
                        "uvIndex": h.get("uvIndex", "N/A"),
                        "cloudcover": h.get("cloudcover", "N/A"),
                        "visibility": h.get("visibility", "N/A"),
                        "wind_speed": h.get("windspeedKmph", "N/A"),
                        "wind_dir": h.get("winddir16Point", "N/A"),
                        "chance_of_rain": h.get("chanceofrain", "0"),
                        "chance_of_snow": h.get("chanceofsnow", "0"),
                        "chance_of_thunder": h.get("chanceofthunder", "0"),
                        "chance_of_fog": h.get("chanceoffog", "0"),
                        "chance_of_frost": h.get("chanceoffrost", "0"),
                        "chance_of_overcast": h.get("chanceofovercast", "0"),
                        "chance_of_sunshine": h.get("chanceofsunshine", "0"),
                        "chance_of_windy": h.get("chanceofwindy", "0"),
                        "chance_of_hightemp": h.get("chanceofhightemp", "0"),
                        "chance_of_remdry": h.get("chanceofremdry", "0"),
                        "DewPointC": h.get("DewPointC", "N/A"),
                        "HeatIndexC": h.get("HeatIndexC", "N/A"),
                        "WindChillC": h.get("WindChillC", "N/A"),
                        "shortRad": h.get("shortRad", "0"),
                        "diffRad": h.get("diffRad", "0"),
                    })
                daily_list = data.get("weather", [])
                daily_data = []
                for day in daily_list[:5]:
                    astro = day.get("astronomy", [{}])[0] if day.get("astronomy") else {}
                    first_hour = day.get("hourly", [{}])[0] if day.get("hourly") else {}
                    daily_data.append({
                        "date": day.get("date", ""),
                        "max": day.get(f"maxtemp{unit.upper()}", "N/A"),
                        "min": day.get(f"mintemp{unit.upper()}", "N/A"),
                        "avg": day.get(f"avgtemp{unit.upper()}", "N/A"),
                        "condition": first_hour.get("weatherDesc", [{}])[0].get("value", ""),
                        "uvIndex": day.get("uvIndex", "N/A"),
                        "sunrise": astro.get("sunrise", ""),
                        "sunset": astro.get("sunset", ""),
                        "moonrise": astro.get("moonrise", ""),
                        "moonset": astro.get("moonset", ""),
                        "moon_phase": astro.get("moon_phase", ""),
                        "moon_illumination": astro.get("moon_illumination", "0"),
                        "chance_of_rain": first_hour.get("chanceofrain", "0"),
                        "chance_of_snow": first_hour.get("chanceofsnow", "0"),
                        "chance_of_thunder": first_hour.get("chanceofthunder", "0"),
                        "chance_of_fog": first_hour.get("chanceoffog", "0"),
                        "chance_of_frost": first_hour.get("chanceoffrost", "0"),
                    })

                result = {
                    "city": city,
                    "unit": unit.upper(),
                    "current": current_data,
                    "hourly": hourly_data,
                    "daily": daily_data,
                }
                return json.dumps(result, ensure_ascii=False)
    except asyncio.TimeoutError:
        return json.dumps({"error": "天气查询超时"}, ensure_ascii=False)
    except Exception as e:
        logger.debug("execute_weather 内部忽略的异常", exc_info=True)
        return json.dumps({"error": f"天气查询异常：{str(e)[:100]}"}, ensure_ascii=False)


# --------------------- news ---------------------
NEWS_FEEDS = {
    "bbc": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",
    "reuters": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    "cna": "https://www.cna.com.tw/rss/cna/rnews.xml",
    "cnn": "http://rss.cnn.com/rss/edition.rss",
    "nytimes": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "guardian": "https://www.theguardian.com/world/rss",
    "zaobao": "https://www.zaobao.com.sg/rss.xml",
    "xinhua": "http://www.xinhuanet.com/english/rss/world.xml",
}

async def execute_news(source: str = "bbc", limit: int = 5) -> str:
    limit = min(max(limit, 1), 10)
    source_key = source.lower()
    if source_key == "all":
        # 8 个 RSS 源并行抓取，总延迟约等于最慢一个源。
        async def _fetch_feed(src: str, url: str) -> list[tuple[str, str, str]]:
            try:
                feed = await asyncio.to_thread(feedparser.parse, url)
                if not feed.bozo:
                    return [(src, item.title, item.link) for item in feed.entries[:min(2, limit)]]
            except Exception as exc:
                logger.warning("news 源抓取失败 src=%s: %s", src, exc)
            return []

        feed_results = await asyncio.gather(
            *(_fetch_feed(src, url) for src, url in NEWS_FEEDS.items())
        )
        all_items = [entry for entries in feed_results for entry in entries]
        if not all_items:
            return "失败：无法获取任何新闻源。"
        lines = ["<ul>"]
        for src, title, link in all_items[:limit*2]:
            lines.append(f'<li><b>{escape_html(title)}</b> (<i>{escape_html(src.upper())}</i>) <a href="{escape_html(link)}">🔗 阅读原文</a></li>')
        lines.append("</ul>")
        return "\n".join(lines)
    url = NEWS_FEEDS.get(source_key)
    if not url:
        return f"失败：不支持的新闻源：{source}。可用：{', '.join(NEWS_FEEDS.keys())} 或 all。"
    try:
        feed = await asyncio.to_thread(feedparser.parse, url)
        if feed.bozo:
            return f"失败：解析新闻源 {source} 失败。"
        items = feed.entries[:limit]
        if not items:
            return f"失败：未找到 {source} 的新闻。"
        lines = ["<ul>"]
        for item in items:
            lines.append(f'<li><b>{escape_html(item.title)}</b> <a href="{escape_html(item.link)}">🔗 阅读原文</a></li>')
        lines.append("</ul>")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("execute_news 内部忽略的异常", exc_info=True)
        return f"失败：新闻获取失败：{str(e)[:100]}"


# --------------------- crypto_price ---------------------
COIN_MAP = {
    "btc": "bitcoin", "eth": "ethereum", "doge": "dogecoin",
    "sol": "solana", "xrp": "ripple", "ada": "cardano",
    "dot": "polkadot", "ltc": "litecoin", "bch": "bitcoin-cash",
    "matic": "matic-network", "avax": "avalanche-2", "uni": "uniswap"
}

async def execute_crypto_price(coin: str, currency: str = "usd") -> str:
    # 安全：coin / currency 直接来自 LLM 工具调用参数，若不 quote
    # 就拼到 URL，LLM 可能传 "btc&ids=ethereum" 之类的字符串做参数注入。
    # 这里强制白名单（coin_id 只允许字母数字和连字符），currency 同理。
    coin_raw = (coin or "").lower().strip()
    currency_raw = (currency or "usd").lower().strip() or "usd"
    if not re.match(r'^[a-z0-9-]+$', coin_raw):
        return f"失败：币种标识 {coin!r} 包含非法字符。"
    if not re.match(r'^[a-z]{3}$', currency_raw):
        return f"失败：货币代码 {currency!r} 必须是 3 个小写字母。"
    coin_id = COIN_MAP.get(coin_raw, coin_raw)
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={quote(coin_id, safe='')}&vs_currencies={quote(currency_raw, safe='')}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=HTTP_TIMEOUT_SHORT) as resp:
                if resp.status != 200:
                    return f"失败：无法获取 {coin} 的价格（HTTP {resp.status}）。"
                data = await resp.json()
    except Exception as e:
        logger.debug("execute_crypto_price 内部忽略的异常", exc_info=True)
        return f"失败：价格查询失败：{str(e)[:100]}"
    if coin_id not in data:
        return f"失败：未找到加密货币：{coin}。支持：{', '.join(COIN_MAP.keys())}"
    price = data[coin_id].get(currency)
    if price is None:
        return f"失败：不支持的目标货币：{currency}"
    return f"<b>{coin.upper()} 当前价格</b><br/>{price} {currency.upper()}"


# --------------------- qr_code ---------------------
async def execute_qr_code(text: str) -> str:
    if not text:
        return "失败：请提供要编码的文本或 URL。"
    if qrcode is None:
        # qrcode 为可选依赖（见文件头部 try/except 导入），缺失时给出
        # 明确失败原因而不是 AttributeError。
        return "失败：二维码组件未安装，请联系管理员安装 qrcode 依赖。"
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    key = f"qr/{hashlib.md5(text.encode()).hexdigest()}.png"
    url = await upload_bytes_to_r2(img_bytes, key, "image/png")
    if url:
        return f"✅ 二维码生成成功\n内容：{text[:200]}\n图片链接：{url}"
    else:
        return "失败：R2 上传失败，请检查配置。"
