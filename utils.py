import json
import re
import aiohttp
import asyncio
from config import BASE_URL, DEEPSEEK_API_KEY, OPENROUTER_API_KEY
from typing import List

deleted_messages = set()
deleted_messages_lock = asyncio.Lock()


import logging
logger = logging.getLogger(__name__)

import logging
logger = logging.getLogger(__name__)

async def send_message(chat_id: int, text: str, max_chars: int = 4096, pre_escaped: bool = False) -> None:
    """Send message to Telegram with HTML formatting"""
    if not text or not text.strip():
        text = "⚠️ No valid content to send"

    if pre_escaped:
        final_text = text
    else:
        final_text = escape_html(text)

    # Verify HTML balance
    if not is_html_balanced(final_text):
        final_text = fix_html_tags(final_text)
        if not is_html_balanced(final_text):
            final_text = sanitize_html(final_text)

    # Final validation
    if "<blockquote" in final_text and "</blockquote>" not in final_text:
        final_text += "</blockquote>"
    if "🔍 <b>Final Answer</b>:" in final_text and not is_html_balanced(final_text):
        final_text = fix_html_tags(final_text)

    # Ensure \n is not escaped as \\n
    final_text = final_text.replace("\\n", "\n")
    logger.debug(f"Sending to Telegram: {repr(final_text)}")  # 添加调试日志，显示实际发送的字符串

    if len(final_text) <= max_chars:
        await _send_single_message(chat_id, final_text, parse_mode="HTML")
        return

    messages = split_message(final_text, max_chars)
    for i, msg in enumerate(messages):
        if "<pre>" in final_text:
            if "<pre>" not in msg[:50] and any(c in msg for c in ("```", "    ", "\t")):
                msg = "<pre>" + msg
            if "</pre>" not in msg[-50:] and "<pre>" in msg[:50]:
                msg = msg + "</pre>"

        # Log each split message
        logger.debug(f"Sending split message {i+1}/{len(messages)}: {repr(msg)}")
        try:
            await _send_single_message(chat_id, msg, parse_mode="HTML")
            await asyncio.sleep(1.0)
        except Exception as e:
            logger.error(f"Failed to send message: {str(e)}")
            for retry in range(2):
                try:
                    await asyncio.sleep(2.0)
                    await _send_single_message(chat_id, msg, parse_mode="HTML")
                    logger.info(f"Retry {retry+1} succeeded")
                    break
                except Exception:
                    logger.error(f"Retry {retry+1} failed")
                    continue

async def _send_single_message(chat_id: int, text: str, parse_mode: str, max_retries: int = 3) -> None:
    """Send single message with retry logic"""
    text = fix_html_tags(text)
    if not is_html_balanced(text):
        text = sanitize_html(text)
    if parse_mode == "HTML":
        text = sanitize_html(text)
        if not is_html_balanced(text):
            text = fix_html_tags(text)

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{BASE_URL}/sendMessage", json=payload) as response:
                    if response.status == 200:
                        return
                    if "can't parse entities" in await response.text():
                        payload["parse_mode"] = ""
                        payload["text"] = strip_html_tags(text)
        except Exception:
            await asyncio.sleep(1)

    try:
        async with aiohttp.ClientSession() as session:
            plain_text = strip_html_tags(text)
            if len(plain_text) > 3000:
                plain_text = plain_text[:3000] + "...(truncated)"
            payload = {"chat_id": chat_id, "text": plain_text}
            async with session.post(f"{BASE_URL}/sendMessage", json=payload):
                pass
    except Exception:
        pass


def is_html_balanced(text: str) -> bool:
    """Check if HTML tags are balanced"""
    stack = []
    tag_pattern = re.compile(r'<(/?)(\w+(?:-\w+)?)(\s+[^>]*)?>', re.DOTALL)

    for match in tag_pattern.finditer(text):
        is_closing = match.group(1) == "/"
        tag_name = match.group(2)

        if match.group(0).endswith("/>"):
            continue

        if is_closing:
            if not stack or stack[-1] != tag_name:
                return False
            stack.pop()
        else:
            stack.append(tag_name)

    return len(stack) == 0


def fix_html_tags(text: str) -> str:
    """Fix unbalanced HTML tags, allow unclosed <code>"""
    text = re.sub(r'</pre>\s*</pre>', '</pre>', text)

    stack = []
    tag_pattern = re.compile(r'<(/?)(\w+(?:-\w+)?)(\s+[^>]*)?>', re.DOTALL)
    result = []
    last_end = 0

    for match in tag_pattern.finditer(text):
        start, end = match.span()
        result.append(text[last_end:start])

        is_closing = match.group(1) == "/"
        tag_name = match.group(2)
        supported_tags = ["b", "strong", "i", "em", "u", "ins", "s", "strike",
                          "del", "a", "code", "pre", "tg-spoiler", "blockquote"]

        if tag_name not in supported_tags:
            result.append(match.group(0))
        elif is_closing:
            if stack and stack[-1] == tag_name:
                result.append(match.group(0))
                stack.pop()
            else:
                found = False
                for i, t in enumerate(stack):
                    if t == tag_name:
                        for inner_tag in reversed(stack[i + 1:]):
                            result.append(f"</{inner_tag}>")
                        result.append(match.group(0))
                        for inner_tag in stack[i + 1:]:
                            result.append(f"<{inner_tag}>")
                        stack.pop(i)
                        found = True
                        break
                if not found:
                    result.append(match.group(0))
        else:
            result.append(match.group(0))
            stack.append(tag_name)
        last_end = end

    result.append(text[last_end:])

    # 只补必要的闭合标签，<code> 可以不闭合
    for tag in reversed(stack):
        if tag != "code":  # 允许 <code> 不闭合
            result.append(f"</{tag}>")

    return "".join(result)


async def send_list_with_timeout(chat_id: int, prompt: str, items: List[str], timeout: int = 8) -> int:
    """Send list with buttons that times out and return message_id"""
    keyboard = {
        "inline_keyboard": [
            [{"text": item, "callback_data": item}] for item in items
        ]
    }
    full_message = escape_html(prompt)
    payload = {
        "chat_id": chat_id,
        "text": full_message,
        "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard)
    }
    message_id = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{BASE_URL}/sendMessage", json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    message_id = result.get("result", {}).get("message_id")
                    if message_id:
                        await asyncio.sleep(timeout)
                        await delete_message(chat_id, message_id)
    except Exception:
        pass
    return message_id


async def delete_message(chat_id: int, message_id: int) -> None:
    """Delete specified message"""
    async with deleted_messages_lock:
        if message_id in deleted_messages:
            return
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{BASE_URL}/deleteMessage",
                        json={"chat_id": chat_id, "message_id": message_id}
                ) as response:
                    if response.status == 200:
                        deleted_messages.add(message_id)
        except Exception:
            pass


def escape_html(text: str) -> str:
    """Escape HTML special chars while preserving Telegram tags and <pre> content"""
    if not text:
        return ""

    supported_tags = [
        "b", "strong", "i", "em", "u", "ins",
        "s", "strike", "del", "a", "code", "pre",
        "tg-spoiler", "blockquote"
    ]

    # 保护 <pre> 内容
    pre_blocks = []
    def store_pre(match):
        pre_blocks.append(match.group(0))
        return f"__PRE_{len(pre_blocks) - 1}__"

    text = re.sub(r'<pre>.*?</pre>', store_pre, text, flags=re.DOTALL)

    # 保护 <a> 标签
    text = re.sub(
        r'<a\s+href="([^"]+)"\s*>([^<]+)</a>',
        lambda m: f'__TEMP_A_START__{m.group(1)}__TEMP_A_MID__{m.group(2)}__TEMP_A_END__',
        text,
        flags=re.IGNORECASE
    )

    # 转义非标签部分
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 恢复支持的标签
    tag_pattern = re.compile(r'&lt;(/?)(\w+)(\s+[^&]*?)?&gt;')

    def replace_tag(match):
        is_closing = match.group(1)
        tag_name = match.group(2).lower()
        if tag_name in supported_tags:
            attrs = match.group(3) or ""
            return f"<{is_closing}{tag_name}{attrs}>"
        return match.group(0)

    text = tag_pattern.sub(replace_tag, text)

    # 恢复 <a> 标签
    text = text.replace('__TEMP_A_START__', '<a href="')
    text = text.replace('__TEMP_A_MID__', '">')
    text = text.replace('__TEMP_A_END__', '</a>')

    # 恢复 <pre> 内容
    for i, block in enumerate(pre_blocks):
        text = text.replace(f"__PRE_{i}__", block)

    return text


def split_message(text: str, max_chars: int) -> List[str]:
    """Split long message while preserving HTML"""
    if len(text) <= max_chars:
        return [text]

    supported_tags = [
        "b", "strong", "i", "em", "u", "ins",
        "s", "strike", "del", "code", "pre",
        "tg-spoiler", "blockquote", "a"
    ]

    parts = []
    current_part = ""
    open_tags_stack = []
    separators = ["\n\n", "\n", ". ", "? ", "! ", "; ", ", "]
    remaining_text = text

    while len(remaining_text) > 0:
        if "<pre>" in remaining_text[:max_chars]:
            pre_start = remaining_text.find("<pre>")
            pre_end = remaining_text.find("</pre>", pre_start)
            if pre_end != -1 and (pre_end - pre_start + 6) <= max_chars:
                code_block = remaining_text[pre_start:pre_end + 6]
                if len(current_part) + len(code_block) <= max_chars:
                    current_part += remaining_text[:pre_end + 6]
                    remaining_text = remaining_text[pre_end + 6:]
                    continue

        best_pos = -1
        for sep in separators:
            pos = remaining_text[:max_chars].rfind(sep)
            if pos > best_pos:
                best_pos = pos + len(sep)
                break

        if best_pos <= 0:
            words = list(re.finditer(r'\b\w+\b', remaining_text[:max_chars]))
            if words:
                last_word = words[-1]
                best_pos = last_word.end()
            else:
                best_pos = min(int(max_chars * 0.8), len(remaining_text))

        tag_start = remaining_text.rfind('<', 0, best_pos)
        tag_end = remaining_text.find('>', tag_start) if tag_start != -1 else -1
        if tag_start != -1 and tag_end != -1 and tag_end > best_pos:
            best_pos = tag_start

        part_to_add = remaining_text[:best_pos]
        remaining_text = remaining_text[best_pos:]

        for match in re.finditer(r'<(/?)(\w+)(\s+[^>]*)?>', part_to_add):
            is_closing = match.group(1) == "/"
            tag_name = match.group(2).lower()
            if tag_name in supported_tags:
                if is_closing:
                    if open_tags_stack and open_tags_stack[-1] == tag_name:
                        open_tags_stack.pop()
                else:
                    open_tags_stack.append(tag_name)

        if current_part:
            current_part += part_to_add
            for tag in reversed(open_tags_stack):
                current_part += f"</{tag}>"
            parts.append(current_part)
            current_part = ""
            for tag in open_tags_stack:
                current_part += f"<{tag}>"
        else:
            parts.append(part_to_add)

    if current_part:
        for tag in reversed(open_tags_stack):
            current_part += f"</{tag}>"
        parts.append(current_part)

    final_parts = []
    for part in parts:
        if not is_html_balanced(part):
            part = fix_html_tags(part)
        if len(part) > max_chars:
            sub_parts = split_message(part, max_chars)
            final_parts.extend(sub_parts)
        else:
            final_parts.append(part)

    return final_parts


def sanitize_html(text: str) -> str:
    """Ensure HTML tag validity, preserve <pre> content"""
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<blockquote\s+expandable>([^<]*)</blockquote>',
                  r'<blockquote expandable>\1</blockquote>', text)

    # 保护 <pre> 内容
    pre_blocks = []
    def store_pre(match):
        pre_blocks.append(match.group(0))
        return f"__PRE_{len(pre_blocks) - 1}__"

    text = re.sub(r'<pre>.*?</pre>', store_pre, text, flags=re.DOTALL)

    stack = []
    tag_pattern = re.compile(r'<(/?)(\w+(?:-\w+)?)(\s+[^>]*)?>', re.DOTALL)
    result = []
    last_end = 0

    for match in tag_pattern.finditer(text):
        start, end = match.span()
        result.append(text[last_end:start])

        is_closing = match.group(1) == "/"
        tag_name = match.group(2)

        if tag_name not in ["b", "strong", "i", "em", "u", "ins", "s", "strike",
                            "del", "a", "code", "pre", "tg-spoiler", "blockquote"]:
            result.append(match.group(0).replace("<", "&lt;").replace(">", "&gt;"))
        elif is_closing:
            if stack and stack[-1][0] == tag_name:
                result.append(match.group(0))
                stack.pop()
            else:
                for i, (t, _) in enumerate(stack):
                    if t == tag_name:
                        for inner_tag, _ in reversed(stack[i + 1:]):
                            result.append(f"</{inner_tag}>")
                        result.append(match.group(0))
                        for inner_tag, attrs in stack[i + 1:]:
                            result.append(f"<{inner_tag}{attrs or ''}>")
                        stack.pop(i)
                        break
        else:
            attrs = match.group(3) or ""
            if tag_name == "a" and not re.search(r'\s+href="[^"]+"', attrs):
                result.append(match.group(0).replace("<", "&lt;").replace(">", "&gt;"))
            else:
                result.append(match.group(0))
                stack.append((tag_name, attrs))

        last_end = end

    result.append(text[last_end:])
    for tag, _ in reversed(stack):
        result.append(f"</{tag}>")

    text = "".join(result)
    # 恢复 <pre> 内容
    for i, block in enumerate(pre_blocks):
        text = text.replace(f"__PRE_{i}__", block)

    return text


def strip_html_tags(text: str) -> str:
    """Remove all HTML tags"""
    text = text.replace("<br/>", "\n").replace("<br>", "\n")
    return re.sub(r'<[^>]*>', '', text)

async def check_deepseek_balance() -> tuple[str, str]:
    """查询 DeepSeek API 的余额，返回 total_balance 和 currency"""
    url = "https://api.deepseek.com/user/balance"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    balance_info = data["balance_infos"][0]
                    total_balance = balance_info["total_balance"]
                    currency = balance_info["currency"]
                    return total_balance, currency
                else:
                    logger.error(f"DeepSeek API 请求失败: {await response.text()}")
                    return None, None
    except Exception as e:
        logger.error(f"查询 DeepSeek 余额时出错: {str(e)}")
        return None, None

# 添加新的 OpenRouter 查询函数
async def check_openrouter_balance() -> float:
    """查询 OpenRouter API 的剩余余额"""
    url = "https://openrouter.ai/api/v1/auth/key"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
                if "data" in data:
                    remaining = data["data"]["limit_remaining"] if data["data"]["limit_remaining"] is not None else 0
                    return remaining
                return 0
    except Exception as e:
        logger.error(f"查询 OpenRouter 余额时出错: {str(e)}")
        return 0
