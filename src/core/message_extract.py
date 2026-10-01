"""入站消息/贴纸文本提取与 Groq 语音转录（自 utils.py 拆出）。"""

import re
from typing import Union

import aiohttp

from config import GROQ_API_KEY

import logging

logger = logging.getLogger(__name__)


def _extract_rich_message_text(rich_obj: Union[dict, list, str]) -> str:
    if isinstance(rich_obj, str):
        return rich_obj
    if isinstance(rich_obj, list):
        parts = []
        for item in rich_obj:
            parts.append(_extract_rich_message_text(item))
        return "".join(parts)
    if isinstance(rich_obj, dict):
        block_type = rich_obj.get("type")
        if block_type == "paragraph":
            return _extract_rich_message_text(rich_obj.get("text", ""))
        elif block_type == "list":
            items = rich_obj.get("items", [])
            item_texts = []
            for item in items:
                label = item.get("label", "")
                blocks = item.get("blocks", [])
                content = _extract_rich_message_text(blocks)
                if label:
                    item_texts.append(f"{label} {content}")
                else:
                    item_texts.append(content)
            return "\n".join(item_texts)
        elif block_type in ("bold", "italic", "underline", "strikethrough", "code", "spoiler"):
            return _extract_rich_message_text(rich_obj.get("text", ""))
        else:
            result = []
            for key, value in rich_obj.items():
                if key in ("text", "blocks", "items"):
                    result.append(_extract_rich_message_text(value))
            return "".join(result)
    return ""

def extract_sticker_metadata(sticker: dict) -> dict:
    """从 Telegram Sticker 对象里抽取**对 LLM 有语义价值**的字段。

    Telegram Bot API 的 Sticker 对象字段（已查证
    https://core.telegram.org/bots/api#sticker 与 changelog）很多，
    但大部分对 LLM 没有意义（file_id / file_unique_id 是不透明 ID，
    width / height / file_size 是数字尺寸，thumbnail / 
    premium_animation / mask_position 是几何 / 文件对象），
    LLM 拿到也只是噪声。本函数只保留 LLM 能真正读懂的语义字段。

    ⚠️ 已确认事实（已查证官方文档与 changelog）：
      - Sticker 对象 **没有** `emoji_list` 字段（该字段只在 InputSticker
        上，即 bot 上传贴纸时使用的请求对象）。Sticker 上 emoji 相关
        的字段只有一个：`emoji`（单个字符串，可选）。
      - Sticker 对象 **没有** `format` 字段；格式由 `is_animated` /
        `is_video` 两个布尔表达。本函数按官方说明派生 format=
        static/animated/video 便于 LLM 阅读。

    输出字段（缺字段的直接跳过，不写入字典；只为 LLM 服务的字段）：
      emoji     : str  - Sticker.emoji 原值（唯一的情感语义信号）
      type      : str  - regular / mask / custom_emoji
      format    : str  - 由 is_animated / is_video 派生为
                          static / animated / video
      set_name  : str  - 贴纸包名（如 "AnimatedEmojis" / "Cats"，
                          包名本身常带语义提示）
    """
    if not isinstance(sticker, dict) or not sticker:
        return {}
    meta = {}
    # emoji：Sticker 唯一的 emoji 字段，单个字符串，可选。
    emoji_value = sticker.get("emoji")
    if emoji_value:
        meta["emoji"] = emoji_value
    # type：regular / mask / custom_emoji。
    type_value = sticker.get("type")
    if type_value:
        meta["type"] = type_value
    # set_name：贴纸包名（可空）。
    set_name = sticker.get("set_name")
    if set_name:
        meta["set_name"] = set_name
    # format：派生字段，Sticker 本身没有，由 is_animated / is_video 合成。
    if sticker.get("is_video"):
        meta["format"] = "video"
    elif sticker.get("is_animated"):
        meta["format"] = "animated"
    else:
        meta["format"] = "static"
    return meta


def sticker_metadata_to_text(sticker: dict) -> str:
    """把 Sticker 元数据渲染成对 LLM 友好的短文本。

    用于：用户直接发贴纸 / 引用回复贴纸 / extract_message_text 占位时，
    把贴纸携带的 emoji 等语义信息显式带到对话里，避免 AI 只看到
    "[贴纸]" 这样的无信息占位。

    只输出对 LLM 有语义价值的字段：emoji / 类型 / 格式 / 贴纸包名。
    """
    meta = extract_sticker_metadata(sticker)
    if not meta:
        return "[贴纸]"
    parts = []
    # emoji 优先放在最前，这是 LLM 唯一能直接看见的情感语义信号。
    if meta.get("emoji"):
        parts.append(f"emoji：{meta['emoji']}")
    type_str = meta.get("type")
    if type_str:
        type_label = {
            "regular": "普通贴纸",
            "mask": "面具贴纸",
            "custom_emoji": "自定义表情贴纸",
        }.get(type_str, type_str)
        parts.append(f"类型：{type_label}")
    if meta.get("format"):
        parts.append(f"格式：{meta['format']}")
    if meta.get("set_name"):
        parts.append(f"贴纸包：{meta['set_name']}")
    return "[贴纸] " + " | ".join(parts) if parts else "[贴纸]"


def extract_message_text(message: dict) -> str:
    if not message:
        return ""
    text = message.get("text")
    if text:
        return text
    caption = message.get("caption")
    if caption:
        return caption
    rich = message.get("rich_message")
    if rich:
        if isinstance(rich, str):
            plain = _rich_message_to_text(rich)
            if plain:
                return plain
        elif isinstance(rich, (dict, list)):
            plain = _extract_rich_message_text(rich)
            if plain:
                return plain
    if message.get("photo") or message.get("video") or message.get("audio") or message.get("document"):
        return "[媒体内容]"
    sticker = message.get("sticker")
    if sticker:
        return sticker_metadata_to_text(sticker)
    if message.get("voice"):
        return "[语音消息]"
    content = message.get("content")
    if isinstance(content, str):
        return content
    return ""

def _rich_message_to_text(rich_content: str) -> str:
    if not rich_content:
        return ""
    text = rich_content.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    def replace_table(match: re.Match[str]) -> str:
        table_html = match.group(0)
        rows = re.findall(r'<tr>(.*?)</tr>', table_html, re.DOTALL)
        lines = []
        for row in rows:
            cells = re.findall(r'<t[dh]>(.*?)</t[dh]>', row, re.DOTALL)
            cell_texts = [re.sub(r'<[^>]+>', '', cell).strip() for cell in cells]
            lines.append("| " + " | ".join(cell_texts) + " |")
        return "\n".join(lines)
    text = re.sub(r'<table[^>]*>.*?</table>', replace_table, text, flags=re.DOTALL)
    def replace_list(match: re.Match[str]) -> str:
        list_html = match.group(0)
        if '<ol' in list_html:
            items = re.findall(r'<li>(.*?)</li>', list_html, re.DOTALL)
            numbered = [f"{i+1}. {re.sub(r'<[^>]+>', '', item).strip()}" for i, item in enumerate(items)]
            return "\n".join(numbered)
        else:
            items = re.findall(r'<li>(.*?)</li>', list_html, re.DOTALL)
            bulleted = [f"• {re.sub(r'<[^>]+>', '', item).strip()}" for item in items]
            return "\n".join(bulleted)
    text = re.sub(r'<(ul|ol)[^>]*>.*?</\1>', replace_list, text, flags=re.DOTALL)
    def replace_details(match: re.Match[str]) -> str:
        details = match.group(0)
        summary = re.search(r'<summary>(.*?)</summary>', details, re.DOTALL)
        summary_text = re.sub(r'<[^>]+>', '', summary.group(1)).strip() if summary else "详情"
        content = re.sub(r'<summary>.*?</summary>', '', details, flags=re.DOTALL)
        content_text = re.sub(r'<[^>]+>', '', content).strip()
        return f"[{summary_text}]\n{content_text}" if content_text else f"[{summary_text}]"
    text = re.sub(r'<details[^>]*>.*?</details>', replace_details, text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r' +', ' ', text)
    text = re.sub(r'\n\s*\n', '\n', text)
    return text.strip()

async def transcribe_audio_with_groq(audio_bytes: bytes, file_ext: str = ".ogg") -> str:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY 未设置，无法转录")

    ext_map = {
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4a-latm",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
    }
    content_type = ext_map.get(file_ext.lower(), "audio/ogg")

    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    data = {
        "model": "whisper-large-v3-turbo",
        "language": "zh",
        "response_format": "json"
    }

    form = aiohttp.FormData()
    form.add_field(
        "file",
        audio_bytes,
        filename=f"audio{file_ext}",
        content_type=content_type
    )
    for key, value in data.items():
        form.add_field(key, str(value))

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=form, timeout=30) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise Exception(f"Groq 转录失败 (HTTP {resp.status}): {err_text[:200]}")
            result = await resp.json()
            return result.get("text", "").strip()
