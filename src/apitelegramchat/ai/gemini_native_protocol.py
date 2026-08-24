"""Gemini 原生协议双向转换器。

OpenAI 兼容消息结构（项目内部 loop_messages 使用的格式）与 Gemini 原生
:streamGenerateContent 端点期望的 contents/systemInstruction/tools 结构之间
的双向转换。

只保留思考过程文本（reasoning_content）和 thought_signature 链，避免走
OpenAI 兼容端点时签名被剥离导致的多轮思考链丢失。

设计原则
--------
1. **零外部依赖**：只用 stdlib + 项目内 utils，方便单测与故障排查。
2. **可逆性**：assistant_msg 携带的 reasoning_content / tool_calls /
   _gemini_thought_signatures 必须能完整还原成 Gemini 原生 parts，反之亦然。
3. **优雅降级**：未识别的 multimodal part（videoCode/inlineData HTTP URL 等）
   不会让整个请求失败，而是退化为纯文本提示。
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from apitelegramchat.utils import get_logger

logger = get_logger(__name__)


# 内部字段名：用于在 OpenAI 兼容 assistant_msg 上挂载 Gemini 私有元数据，
# 以便下一轮请求重建原生 thought parts。前缀 "_" 表示这是协议适配层的
# 私有约定，不应出现在最终持久化历史中（但即使保留也无副作用）。
_GEMINI_THOUGHT_SIGNATURES_KEY = "_gemini_thought_signatures"


# ---------------------------------------------------------------------------
# 请求方向：OpenAI messages -> Gemini contents
# ---------------------------------------------------------------------------

def _extract_text_from_openai_content(content: Any) -> str:
    """OpenAI content 可能是 str 或 list[{type, text/image_url/...}]。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            # image_url / input_audio 在 _parts_from_openai_user_message 里单独处理
        return "\n".join(p for p in parts if p)
    return str(content)


def _inline_data_from_image_url(url: str) -> dict | None:
    """把 data:image/png;base64,XXX 内联成 Gemini inlineData。"""
    match = re.match(r"data:([\w/\-.+]+);base64,(.+)", url, re.DOTALL)
    if not match:
        return None
    mime_type, b64 = match.group(1), match.group(2)
    try:
        # 校验可解码；inlineData 字段要求是 base64 字符串本身，不是 bytes。
        base64.b64decode(b64, validate=True)
        return {"inlineData": {"mimeType": mime_type, "data": b64}}
    except Exception as exc:
        logger.warning("无法解码内联图片 data URL，跳过: %s", exc)
        return None


def _parts_from_openai_user_message(msg: dict) -> list[dict]:
    """role=user 的消息：text + image_url(inlineData) 混合。"""
    content = msg.get("content")
    parts: list[dict] = []
    if isinstance(content, str):
        if content:
            parts.append({"text": content})
        return parts
    if not isinstance(content, list):
        parts.append({"text": str(content or "")})
        return parts

    for item in content:
        if not isinstance(item, dict):
            parts.append({"text": str(item)})
            continue
        itype = item.get("type")
        if itype == "text":
            text = str(item.get("text") or "")
            if text:
                parts.append({"text": text})
        elif itype in ("image_url", "image"):
            url = ""
            iu = item.get("image_url")
            if isinstance(iu, dict):
                url = str(iu.get("url") or "")
            else:
                url = str(iu or "")
            if not url:
                continue
            inline = _inline_data_from_image_url(url)
            if inline is not None:
                parts.append(inline)
            else:
                # 非 data: URL 的外部 HTTP 图片 Gemini 原生端点无法直接接受，
                # 退化为文本占位以避免整个请求 400。
                parts.append({"text": f"[image: {url}]"})
        elif itype == "input_audio":
            ia = item.get("input_audio") or {}
            b64 = ia.get("data") or ""
            if b64:
                parts.append({"inlineData": {"mimeType": ia.get("format") or "audio/mpeg", "data": b64}})
        elif itype and itype not in ("text",):
            # 未识别类型退化为文本，保证消息不丢
            parts.append({"text": f"[{itype}]"})
    if not parts:
        parts.append({"text": ""})
    return parts


def _parts_from_openai_assistant_message(msg: dict) -> list[dict]:
    """role=assistant 的消息：reasoning_content(thought) + content(text) + tool_calls(functionCall)。

    重建顺序与 Gemini 原生生成的顺序一致：
      1. thought parts（带 thoughtSignature，若有）
      2. 可见正文 text part
      3. functionCall parts
    """
    parts: list[dict] = []

    reasoning = msg.get("reasoning_content") or ""
    if isinstance(reasoning, str) and reasoning:
        # 注意：reasoning_content 是模型思考的累积文本。Gemini 原生流式里每个 thought
        # part 通常对应一小段思考，并各自带 thoughtSignature。这里只保存了文本和签名
        # 列表，无法精确还原"哪段思考对应哪个签名"——但 Gemini 接受一个完整的 thought
        # part 携带最后一个签名（即 summary signature），仍能通过下一轮签名校验。
        sigs = msg.get(_GEMINI_THOUGHT_SIGNATURES_KEY) or []
        thought_part: dict[str, Any] = {"thought": True, "text": reasoning}
        if sigs:
            thought_part["thoughtSignature"] = sigs[-1]
        parts.append(thought_part)

    content_text = msg.get("content")
    if isinstance(content_text, str) and content_text:
        parts.append({"text": content_text})
    elif isinstance(content_text, list):
        # 极少数兼容网关会把 assistant content 也写成 list
        merged = _extract_text_from_openai_content(content_text)
        if merged:
            parts.append({"text": merged})

    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments", "")
        try:
            args = json.loads(raw_args) if raw_args else {}
            if not isinstance(args, dict):
                args = {"value": args}
        except (json.JSONDecodeError, TypeError):
            args = {"_raw": str(raw_args)[:2000]}
        parts.append({"functionCall": {"name": name, "args": args}})

    return parts


def _parts_from_openai_tool_message(msg: dict) -> list[dict]:
    """role=tool 的消息 → Gemini role=user 的 functionResponse part。"""
    name = msg.get("name") or "unknown"
    raw = msg.get("content") or ""
    if isinstance(raw, (dict, list)):
        response = raw if isinstance(raw, dict) else {"value": raw}
    else:
        text = str(raw)
        try:
            parsed = json.loads(text)
            response = parsed if isinstance(parsed, dict) else {"result": text}
        except (json.JSONDecodeError, TypeError):
            response = {"result": text}
    return [{"functionResponse": {"name": name, "response": response}}]


def openai_messages_to_gemini_contents(
    messages: list[dict],
) -> tuple[dict | None, list[dict]]:
    """把 OpenAI 兼容 messages 列表转成 (systemInstruction, contents)。

    - role=system     → systemInstruction.parts[{text}]（多条 system 合并）
    - role=user        → contents[{role:user, parts}]
    - role=assistant   → contents[{role:model, parts}]  (含 thought/text/functionCall)
    - role=tool        → contents[{role:user, parts:[{functionResponse}]}]
    - role=assistant+tool_calls 紧随其后的 role=tool 消息：原生协议要求把 tool
      response 直接放在 user role 后；本转换保持 OpenAI 顺序，Gemini 端能正确识别。
    """
    system_parts: list[dict] = []
    contents: list[dict] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role == "system":
            text = _extract_text_from_openai_content(msg.get("content"))
            if text:
                system_parts.append({"text": text})
            continue

        if role == "user":
            parts = _parts_from_openai_user_message(msg)
            contents.append({"role": "user", "parts": parts})
            continue

        if role == "assistant":
            parts = _parts_from_openai_assistant_message(msg)
            if not parts:
                parts = [{"text": ""}]
            contents.append({"role": "model", "parts": parts})
            continue

        if role == "tool":
            parts = _parts_from_openai_tool_message(msg)
            contents.append({"role": "user", "parts": parts})
            continue

        # 未知 role，退化为 user 文本，避免丢消息
        contents.append({"role": "user", "parts": [{"text": json.dumps(msg, ensure_ascii=False)[:2000]}]})

    system_instruction = {"parts": system_parts} if system_parts else None
    return system_instruction, contents


def openai_tools_to_gemini_function_declarations(tools: list[dict] | None) -> list[dict] | None:
    """OpenAI tools -> Gemini tools[{functionDeclarations:[...]}]。"""
    if not tools:
        return None
    decls: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") or {}
        if not fn.get("name"):
            continue
        decl: dict[str, Any] = {
            "name": fn["name"],
            "description": fn.get("description") or "",
        }
        params = fn.get("parameters")
        if isinstance(params, dict):
            # Gemini 接受 OpenAI 风格 JSON Schema 子集；直接透传，但去掉 $schema 等
            # 非必要字段以避免某些版本端点的拒绝。
            cleaned = {k: v for k, v in params.items() if k != "$schema"}
            decl["parameters"] = cleaned
        decls.append(decl)
    if not decls:
        return None
    return [{"functionDeclarations": decls}]


# ---------------------------------------------------------------------------
# 响应方向：Gemini 原生 chunk parts -> OpenAI 兼容内部状态
# ---------------------------------------------------------------------------

def merge_gemini_part_into_state(
    part: dict,
    state: dict,
) -> None:
    """把单个 Gemini part 合并到累计状态字典。

    state 字段：
      - reasoning_acc: str  (thought=true 的 text 累积)
      - content_acc: str   (thought=false 或无 thought 的 text 累积)
      - tool_calls: list[dict]  (OpenAI 兼容格式)
      - thought_signatures: list[str]  (按出现顺序)
      - finish_reason: str | None
    """
    if not isinstance(part, dict):
        return

    # 1) 思考 part
    if part.get("thought") is True:
        text = part.get("text") or ""
        if text:
            state["reasoning_acc"] += text
        sig = part.get("thoughtSignature")
        if sig:
            state["thought_signatures"].append(sig)
        return

    # 2) 普通 text part（thought=false 或缺省）
    if "text" in part:
        state["content_acc"] += part.get("text") or ""
        return

    # 3) functionCall part → OpenAI tool_call
    fc = part.get("functionCall")
    if isinstance(fc, dict):
        name = fc.get("name") or ""
        args = fc.get("args") or {}
        if not isinstance(args, dict):
            args = {"value": args}
        try:
            args_str = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            args_str = "{}"
        # Gemini 原生 functionCall 没有显式 id，需要合成一个稳定的 id
        # 用于 builder.add_tool_item 的去重。同一轮内若有同名调用，附加索引。
        tc_id = f"callgemini_{state.get('_round', 0)}_{len(state['tool_calls'])}"
        state["tool_calls"].append({
            "id": tc_id,
            "type": "function",
            "function": {"name": name, "arguments": args_str},
        })
        return

    # 4) thoughtSignature 单独 part（罕见，但 Google 文档允许）
    if "thoughtSignature" in part and not part.get("text"):
        sig = part.get("thoughtSignature")
        if sig:
            state["thought_signatures"].append(sig)
        return

    # 5) executableCode / codeExecutionResult / videoMetadata 等扩展：
    #    退化为文本展示，避免草稿缺内容
    if "executableCode" in part:
        ec = part.get("executableCode") or {}
        lang = ec.get("language") or "code"
        code = ec.get("code") or ""
        if code:
            state["content_acc"] += f"\n```{lang}\n{code}\n```\n"
        return
    if "codeExecutionResult" in part:
        cer = part.get("codeExecutionResult") or {}
        outcome = cer.get("outcome") or ""
        output = cer.get("output") or ""
        if output:
            state["content_acc"] += f"\n```\n{output}\n```\n"
        if outcome and outcome != "OUTCOME_OK":
            state["content_acc"] += f"\n[code execution: {outcome}]\n"
        return

    # 未识别 part：debug 日志即可，不抛异常
    logger.debug("未识别的 Gemini part 类型，跳过: %s", list(part.keys()))


def build_assistant_msg_from_gemini_state(state: dict) -> dict:
    """流式收尾时构建 OpenAI 兼容 assistant_msg，含 reasoning_content 与签名链。"""
    content_acc = state.get("content_acc") or ""
    reasoning_acc = state.get("reasoning_acc") or ""
    tool_calls = state.get("tool_calls") or []
    sigs = state.get("thought_signatures") or []

    msg: dict = {"role": "assistant"}
    if content_acc:
        msg["content"] = content_acc
    else:
        msg["content"] = None
    if tool_calls:
        msg["tool_calls"] = [
            {"id": tc["id"], "type": "function",
             "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
            for tc in tool_calls
        ]
    if reasoning_acc:
        msg["reasoning_content"] = reasoning_acc
    if sigs:
        msg[_GEMINI_THOUGHT_SIGNATURES_KEY] = sigs
    return msg
