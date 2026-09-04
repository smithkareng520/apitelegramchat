"""Gemini 原生 API 桥接层（streamGenerateContent SSE 流式 + 原生 function calling）。

设计原则（与 anthropic_bridge.py 完全同构，务必先读，修改本文件前请确保理解）：
====================================================================
1. 全局对话历史（app.py:update_conversation_and_ledger 写入的
   ctx["conversation_history"]）是所有厂商共享的单一存储，且用户可以
   随时在任意两次发言之间切换模型/厂商。因此持久化进历史的消息，
   必须始终是项目原有的 OpenAI 兼容形状：
     {"role": "user"/"assistant"/"tool"/"system", "content": ..., ...}
   绝不能把 Gemini 原生的 contents / parts / functionCall 形状写回
   new_history_entries / loop_messages 传给调用方。

2. 因此本文件的策略是"边界转换"：
   - _agentic_loop_gemini_native 接收到的 messages 参数、以及它追加进
     new_history_entries 的内容，全部是 OpenAI 形状（与
     _agentic_loop_openai_compat / _agentic_loop_anthropic 完全一致），
     可以直接复用 tool_call_loop._run_tool_calls_and_append。
   - 仅在"即将调用 Gemini 原生 API"之前，把当前累积的 OpenAI 形状
     loop_messages 转换成 Gemini 原生的 {systemInstruction, contents}
     形状（_convert_messages_to_gemini）；工具 schema 转换与清洗见
     _convert_tools_to_gemini。
   - Gemini 返回的内容在写回 loop_messages / new_history_entries 前，
     统一转换回 OpenAI 形状（content 字符串 + tool_calls 列表），
     与其它两条循环完全同构，下游 _run_tool_calls_and_append /
     turn_recovery / update_conversation_and_ledger 都无需改动。

3. 协议选择（重要）：
   - 使用 Gemini 原生 v1beta generateContent 协议
     （models/{model}:streamGenerateContent?alt=sse），**绝不**经过
     OpenAI 兼容端点（v1beta/openai/）模拟流式。
   - 传输层沿用本项目 Gemini 既有路径的 aiohttp 直连（与原生图片/
     视频循环、旧 Gemini 兼容循环一致），不引入 google-genai SDK
     新依赖；SSE 解析按当前官方流式响应结构实现（每个 data: 行一个
     GenerateContentResponse JSON，functionCall part 为完整对象，
     不存在跨 chunk 的参数增量拼接）。
   - thought signature：原生 API 在 functionCall part 上返回
     thoughtSignature，下一轮请求必须原样回传。历史存储沿用既有
     OpenAI 形状里的 tc["thought_signature"] /
     tc["extra_content"]["google"]["thought_signature"] 双字段格式
     （与旧 Gemini 兼容循环完全一致），转换时还原到原生 part 上，
     保证切换模型/重启后历史语义不丢。

这样即使用户上一轮用的是 Gemini，下一轮切换回 OpenAI 兼容厂商或
Claude，历史读出来仍是标准 OpenAI 形状，不会导致任何其它厂商的请求
出错——完全不影响现有项目行为。
"""
import asyncio
import copy
import json
import uuid
from typing import TYPE_CHECKING, Optional

import aiohttp

from apitelegramchat.config import (
    GEMINI_API_KEY,
    SUPPORTED_MODELS,
    get_sampling_params,
    get_reasoning_request_fields,
)
from apitelegramchat.utils import get_logger
from apitelegramchat.chat_actions import start_chat_action, stop_chat_action

from apitelegramchat.ai._constants import MAX_TOOL_CALLS, TIMEOUT
from apitelegramchat.ai.cache_usage import _log_cache_usage
from apitelegramchat.ai.tool_summary import (
    _contains_textual_tool_call,
    _generate_action_description,
    _generate_initial_tool_summary,
    _normalize_tool_call_arguments,
    _strip_textual_tool_calls,
    _tool_limit_summary,
)
from apitelegramchat.ai.tool_call_loop import _run_tool_calls_and_append
from apitelegramchat.ai.gemini_cache import manager as _gemini_cache_manager

if TYPE_CHECKING:
    from apitelegramchat.ai.rich_message_builder import RichMessageBuilder

logger = get_logger(__name__)

# Gemini 原生 API 基址（v1beta generateContent 协议；非 OpenAI 兼容层）。
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


# =============================================================================
# 工具 schema 转换：OpenAI function-calling 形状 -> Gemini functionDeclarations
# =============================================================================
# OpenAI:   {"type": "function", "function": {"name", "description", "parameters"}}
# Gemini:   [{"functionDeclarations": [{"name", "description", "parameters"}]}]
#
# Gemini 的 parameters 是 OpenAPI 3.0 schema 子集，未声明字段（如
# additionalProperties / $schema / minLength）会导致整请求 400
# （"Unknown name ..."），因此这里做**白名单递归清洗**：只保留官方
# Schema 对象确认支持的字段，宁可丢一个校验提示，也不冒 400 风险。
_GEMINI_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum", "items",
    "properties", "required", "minimum", "maximum",
    "minItems", "maxItems", "minProperties", "maxProperties",
    "anyOf", "title", "example", "default", "propertyOrdering",
}

_MAX_SCHEMA_DEPTH = 12


def _split_union_type(schema: dict, depth: int) -> dict:
    """把 JSON Schema 的数组型 type（union type）转成 Gemini 能表达的形状。

    Gemini 原生 Schema 的 type 是单值枚举，null 用独立的 nullable 布尔
    表达。项目现有工具 schema 存在 "type": ["string", "array"] 这类
    union（如 web_search 的 mode 字段），需要归一：

    - [X, "null"]            -> type=X + nullable=true
    - [X, Y, ...] 多真实类型 -> anyOf: [按类型分拆的子 schema...]
      （items 只随 array 子 schema 走、enum 只随 string 子 schema 走、
       minimum/maximum 只随数值子 schema 走，description 提升到包装层）
    """
    t = schema.get("type")
    if not isinstance(t, list):
        return schema
    types = [str(x).strip().lower() for x in t if str(x).strip()]
    has_null = "null" in types
    real_types = [x for x in types if x and x != "null"]
    if not real_types:
        # 退化：只有 null / 空数组——剥掉 type，保留其余字段。
        return {k: v for k, v in schema.items() if k != "type"}
    if len(real_types) == 1:
        out = {k: v for k, v in schema.items() if k != "type"}
        out["type"] = real_types[0]
        if has_null:
            out["nullable"] = True
        return out

    def _sub_for(real_type: str) -> dict:
        sub: dict = {"type": real_type}
        if schema.get("description"):
            sub["description"] = schema["description"]
        if real_type == "array" and isinstance(schema.get("items"), dict):
            sub["items"] = _clean_schema_for_gemini(schema["items"], depth + 1)
        if real_type == "string" and "enum" in schema:
            sub["enum"] = schema["enum"]
        if real_type in ("integer", "number"):
            for key in ("minimum", "maximum"):
                if key in schema:
                    sub[key] = schema[key]
        if has_null:
            sub["nullable"] = True
        return sub

    out: dict = {}
    if schema.get("description"):
        out["description"] = schema["description"]
    # default 提升到包装层（拆分后的任一子 schema 都不携带语义冲突的默认值）。
    if "default" in schema:
        out["default"] = schema["default"]
    out["anyOf"] = [_sub_for(rt) for rt in real_types]
    return out


def _repair_anyof_branch_required(sub: dict, parent_raw_props: Optional[dict],
                                  depth: int) -> dict:
    """重建 anyOf 分支的 required，使其满足 Gemini 硬校验不变式。

    Gemini 要求任意层级的 ``required`` 中每个名字都必须在**同一层**
    ``properties`` 中有定义，否则整个请求 400（线上实测 2026-09-05，
    gemini-3.5-flash-lite）::

        parameters.any_of[0].required[0]: property is not defined

    JSON Schema 惯用的条件必填写法 ``anyOf: [{"required": ["query"]},
    {"required": ["image_url"]}]``（分支只声明属性名、属性定义留给父级，
    OpenAI / Anthropic 侧完全合法）在 Gemini 必炸。

    修复：把每个分支重建为**自包含 object**——从父级原始 properties
    取回被引用属性的定义补进分支；父级也没有的名字只能剔除（宁可丢
    一条约束，不冒 400 风险）。全部名字都无法落地时分支的 required
    整体移除，退化分支由调用方剔除。

    注意：必须在递归清洗**之前**调用——清洗后置校验看不到父级属性，
    先清洗会把 required 误删。
    """
    req = sub.get("required")
    if not isinstance(req, list) or not req:
        return sub
    branch_props = sub.get("properties")
    if not isinstance(branch_props, dict):
        branch_props = None
    kept: list = []
    injected: dict = {}
    for name in req:
        if not isinstance(name, str):
            continue
        if branch_props is not None and name in branch_props:
            kept.append(name)
        elif parent_raw_props and name in parent_raw_props:
            prop_schema = parent_raw_props[name]
            injected[name] = (
                _clean_schema_for_gemini(prop_schema, depth + 1)
                if isinstance(prop_schema, dict) else {"type": "string"}
            )
            kept.append(name)
    if not kept:
        sub.pop("required", None)
        return sub
    sub["required"] = kept
    if injected:
        if branch_props is None:
            branch_props = {}
            sub["properties"] = branch_props
        branch_props.update(injected)
    return sub


def _clean_schema_for_gemini(schema, depth: int = 0) -> dict:
    """递归清洗一个 JSON Schema 片段为 Gemini 原生 Schema 子集。

    清洗结果保证 Gemini 两条硬校验不变式在任意层级成立（违反即整请求
    400，线上实测见 _repair_anyof_branch_required docstring）：

    1. ``required`` ⊆ 同层 ``properties``：定义不明的 required 名字剔除，
       空 / 畸形 required 整体移除；
    2. 声明 required / properties 的子 schema 必须带 ``type: "object"``。

    另：空 ``properties: {}`` 与不写等价，一并剥掉，避开部分 Gemini
    版本对 OBJECT 空属性的严格校验。

    本函数是**纯函数**：入口深拷贝（仅 depth==0，递归复用已拷贝树），
    绝不修改调用方原始 schema——SEARCH_TOOLS 等模块级常量同时被
    OpenAI / Anthropic 路径共享，原地修改会造成跨 provider 污染。
    """
    if not isinstance(schema, dict):
        return {}
    if depth == 0:
        schema = copy.deepcopy(schema)
    if depth > _MAX_SCHEMA_DEPTH:
        # 超深 schema 防御：保语义占位，不让畸形输入打穿请求。
        return {"type": "string", "description": "(schema 嵌套过深，已降级为字符串)"}
    schema = _split_union_type(schema, depth)
    out: dict = {}
    for key, value in schema.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned_props = {}
            for prop_name, prop_schema in value.items():
                if isinstance(prop_schema, dict):
                    cleaned_props[prop_name] = _clean_schema_for_gemini(prop_schema, depth + 1)
                else:
                    cleaned_props[prop_name] = {"type": "string"}
            out["properties"] = cleaned_props
        elif key == "items" and isinstance(value, dict):
            out["items"] = _clean_schema_for_gemini(value, depth + 1)
        elif key == "anyOf" and isinstance(value, list):
            # 分支自包含修复所需的父级原始 properties（未清洗态）。
            parent_raw_props = schema.get("properties")
            if not isinstance(parent_raw_props, dict):
                parent_raw_props = None
            cleaned_subs = []
            for sub in value:
                if not isinstance(sub, dict):
                    continue
                # 先修 required（原始层，能看见父级属性），再递归清洗；
                # 顺序不能反，原因见 _repair_anyof_branch_required docstring。
                sub = _repair_anyof_branch_required(sub, parent_raw_props, depth)
                sub = _clean_schema_for_gemini(sub, depth + 1)
                # 只有 required / properties 而没有 type 的子 schema：
                # Gemini 的 Schema 子对象缺 type 会被拒，补全为 object。
                if "type" not in sub and ("required" in sub or "properties" in sub):
                    sub["type"] = "object"
                if sub:
                    cleaned_subs.append(sub)
            if cleaned_subs:
                out["anyOf"] = cleaned_subs
        else:
            out[key] = value
    # 硬校验不变式 1：required ⊆ properties（对任意层级生效，含顶层
    # parameters；anyOf 分支已在上方单独修复）。
    req = out.get("required")
    if req is not None:
        props = out.get("properties")
        kept_req = [
            n for n in (req if isinstance(req, list) else [])
            if isinstance(n, str) and isinstance(props, dict) and n in props
        ]
        if kept_req:
            out["required"] = kept_req
        else:
            out.pop("required", None)
    # 空 properties 与不写 properties 等价，剥掉以避开严格校验。
    if isinstance(out.get("properties"), dict) and not out["properties"]:
        out.pop("properties", None)
    return out


def _convert_tools_to_gemini(tools: Optional[list]) -> Optional[list]:
    """OpenAI 工具定义列表 -> Gemini tools=[{functionDeclarations: [...]}]。

    从零重建声明（而非清洗原 dict），因此 input_examples 等函数级
    附加字段天然被排除（等价于旧 _clean_tools_for_gemini 的剔除语义）。
    无法转换 / 无 name 的条目跳过；全部失败返回 None（请求不带 tools）。
    """
    if not tools:
        return None
    declarations = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function", {})
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        decl: dict = {
            "name": str(name),
            "description": fn.get("description", "") or "",
        }
        params = fn.get("parameters")
        if isinstance(params, dict) and params:
            cleaned_params = _clean_schema_for_gemini(params)
            # 清洗后可能只剩空壳（如畸形 schema 全字段被剥）——
            # Gemini 对空 parameters 不报错，但省略更干净。
            if cleaned_params:
                decl["parameters"] = cleaned_params
        declarations.append(decl)
    return [{"functionDeclarations": declarations}] if declarations else None


# =============================================================================
# 消息格式转换：OpenAI 形状 -> Gemini 原生 (systemInstruction, contents)
# =============================================================================
# 规则（与 anthropic_bridge 同构的边界转换）：
#   - role=system -> 拼接进顶层 systemInstruction（Gemini 无 system 角色）
#   - role=user   -> role=user，content parts 转原生 part
#                    （text / inlineData / fileData）
#   - role=assistant -> role=model；tool_calls 还原为 functionCall part
#                    （args 解析为对象；thoughtSignature 原样回传）
#   - role=tool   -> 攒为下一条 user 消息的 functionResponse part
#                    （Gemini 要求 function response 以 user 角色出现；
#                     连续多条 tool 结果必须合并进同一个 user turn）
def _extract_thought_signature(tc: dict) -> str:
    """从 OpenAI 形状的 tool_call 条目里取回 thought signature。

    沿用旧 Gemini 循环的双字段存储格式：tc["thought_signature"] 与
    tc["extra_content"]["google"]["thought_signature"]，任一存在即可。
    """
    if not isinstance(tc, dict):
        return ""
    sig = tc.get("thought_signature")
    if sig is None:
        extra = tc.get("extra_content") or {}
        if isinstance(extra, dict):
            sig = (extra.get("google") or {}).get("thought_signature")
    return str(sig) if sig else ""


_EXT_MIME_MAP = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif", "heic": "image/heic",
    "heif": "image/heif",
    "mp4": "video/mp4", "mpeg": "video/mpeg", "mov": "video/quicktime",
    "webm": "video/webm", "mkv": "video/x-matroska",
    "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/opus",
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
}


def _guess_mime_from_url(url: str, fallback: str) -> str:
    """按扩展名尽力推断 MIME（失败回退到调用方给定的默认值）。"""
    try:
        path = str(url).split("?", 1)[0].split("#", 1)[0]
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        return _EXT_MIME_MAP.get(ext, fallback)
    except Exception:
        return fallback


def _data_url_to_inline_data(url: str) -> Optional[dict]:
    """data:<mime>;base64,<data> -> Gemini inlineData part（失败返回 None）。"""
    try:
        header, b64 = url.split(",", 1)
        mime = header.split(";", 1)[0].split(":", 1)[1] or "application/octet-stream"
    except (ValueError, IndexError):
        return None
    if not b64:
        return None
    return {"inlineData": {"mimeType": mime, "data": b64}}


def _openai_content_to_gemini_parts(content) -> list:
    """把 OpenAI 的 content（str 或 content-parts 列表）转换成 Gemini
    原生 parts 列表。支持的 part 类型：text / image_url（data:base64
    内联与 http(s) 公开 URL）/ video_url / input_audio；未识别的类型
    退化为文本占位，不中断请求。
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []

    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = str(part.get("text") or "")
            if text:
                parts.append({"text": text})
        elif ptype == "image_url":
            url_obj = part.get("image_url") or {}
            url = url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj)
            if url.startswith("data:"):
                inline = _data_url_to_inline_data(url)
                if inline:
                    parts.append(inline)
            elif url:
                # 公开 URL：Gemini 原生支持 fileData.fileUri 引用公网图片
                # （免下载、免 base64 内联，与原生多模态语义一致）。
                parts.append({"fileData": {
                    "fileUri": url,
                    "mimeType": _guess_mime_from_url(url, "image/jpeg"),
                }})
        elif ptype == "video_url":
            url_obj = part.get("video_url") or {}
            url = url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj)
            if url:
                parts.append({"fileData": {
                    "fileUri": url,
                    "mimeType": _guess_mime_from_url(url, "video/mp4"),
                }})
        elif ptype == "input_audio":
            audio = part.get("input_audio") or {}
            data = str(audio.get("data") or "")
            fmt = str(audio.get("format") or "ogg").lower().lstrip(".")
            if fmt == "oga":
                fmt = "ogg"
            if data:
                parts.append({"inlineData": {
                    "mimeType": f"audio/{fmt}",
                    "data": data,
                }})
        elif ptype == "file":
            # 原生文档 part 仅 Anthropic（native_document=True）启用；
            # Gemini 当前模型未开启该能力，防御性降级为文本占位。
            parts.append({"text": "[收到一个文档附件，当前模型不支持原生文档输入]"})
        else:
            parts.append({"text": f"[不支持的内容类型: {ptype or 'unknown'}]"})
    return parts


def _tool_name_for_call_id(messages: list, tool_call_id: str) -> str:
    """从既有消息里回溯 tool_call_id 对应的函数名（functionResponse.name
    必须与 functionCall.name 一致，否则 Gemini 拒绝配对）。"""
    if not tool_call_id:
        return ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for tc in (msg.get("tool_calls") or []):
            if isinstance(tc, dict) and tc.get("id") == tool_call_id:
                fn = tc.get("function") or {}
                return str(fn.get("name") or "")
    return ""


def _merge_consecutive_contents(contents: list) -> list:
    """合并相邻同角色 contents（Gemini 要求 user/model 严格交替）。

    最典型场景：多条 role=tool 聚成一条 user 后，紧跟的真实 user 消息
    会形成连续两个 user turn；v1beta 对连续同角色 contents 返回 400
    （"alternates between user and model"），必须合并 parts。
    """
    merged: list = []
    for content in contents:
        if merged and merged[-1].get("role") == content.get("role"):
            merged[-1]["parts"] = merged[-1].get("parts", []) + content.get("parts", [])
        else:
            merged.append(content)
    return merged


def _convert_messages_to_gemini(messages: list) -> tuple:
    """把 OpenAI 形状的消息列表转换成 Gemini 的
    (system_instruction_text, contents)。

    contents 保证：user/model 角色严格交替、至少一条 content、结尾为
    user 或 functionResponse（即下一轮可直接请求 model 回复）。
    """
    system_parts: list = []
    contents: list = []
    pending_function_responses: list = []

    def _flush_function_responses():
        if pending_function_responses:
            contents.append({"role": "user", "parts": list(pending_function_responses)})
            pending_function_responses.clear()

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            c = msg.get("content")
            if isinstance(c, str) and c:
                system_parts.append(c)
            elif isinstance(c, list):
                for part in c:
                    if (isinstance(part, dict) and part.get("type") == "text"
                            and part.get("text")):
                        system_parts.append(part["text"])
            continue

        if role == "tool":
            name = str(msg.get("name") or "") or _tool_name_for_call_id(
                messages, msg.get("tool_call_id", ""))
            content = msg.get("content", "")
            text = content if isinstance(content, str) else json.dumps(
                content, ensure_ascii=False)
            # response 必须是 JSON 对象：统一包一层 result（官方示例口径）。
            pending_function_responses.append({
                "functionResponse": {
                    "name": name or "unknown_function",
                    "response": {"result": text},
                }
            })
            continue

        _flush_function_responses()

        if role == "user":
            parts = _openai_content_to_gemini_parts(msg.get("content"))
            if parts:
                contents.append({"role": "user", "parts": parts})
            continue

        if role == "assistant":
            parts = []
            text_content = msg.get("content")
            if isinstance(text_content, str) and text_content:
                parts.append({"text": text_content})
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                try:
                    call_args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    call_args = {}
                if not isinstance(call_args, dict):
                    call_args = {"value": call_args}
                call_part: dict = {
                    "functionCall": {"name": fn.get("name", ""), "args": call_args}
                }
                sig = _extract_thought_signature(tc)
                if sig:
                    call_part["thoughtSignature"] = sig
                parts.append(call_part)
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue

    _flush_function_responses()

    contents = _merge_consecutive_contents(contents)
    if not contents:
        # 理论上不可达（调用方永远保证至少一条 user 消息）；防御兜底。
        contents = [{"role": "user", "parts": [{"text": "(empty)"}]}]

    system_instruction = "\n\n".join(p for p in system_parts if p)
    return system_instruction, contents


# =============================================================================
# 推理控制：config 统一出口（get_reasoning_request_fields）-> 原生 thinkingConfig
# =============================================================================
# config.py 的 gemini 分支产出 OpenAI 兼容层形状（顶层 reasoning_effort +
# extra_body.google.thinking_config.thinkingBudget），本桥接在其上解码为
# 原生 generationConfig.thinkingConfig，保证推理控制仍以 config.py 为
# 单一数据源（不在循环内硬编码预算/档位）：
#   reasoning_effort      -> thinkingLevel（Gemini 3 系档位：low/medium/high）
#   enabled=False         -> thinkingBudget=0（显式关闭思考）
#   reasoning_max_tokens  -> thinkingBudget
#   其余                  -> 不发送 thinkingConfig（跟随供应商默认）
def _gemini_thinking_config(model_info) -> Optional[dict]:
    if not model_info:
        return None
    reasoning_top, reasoning_extra = get_reasoning_request_fields(model_info, "gemini")
    google_cfg = {}
    if isinstance(reasoning_extra, dict):
        google = reasoning_extra.get("google")
        if isinstance(google, dict):
            cfg = google.get("thinking_config")
            if isinstance(cfg, dict):
                google_cfg = dict(cfg)
    thinking: dict = dict(google_cfg)
    effort = reasoning_top.get("reasoning_effort") if isinstance(reasoning_top, dict) else None
    if effort:
        thinking["thinkingLevel"] = str(effort)
    if not thinking:
        return None
    # 关闭思考（thinkingBudget=0）时不能再请求思考摘要，其余情况默认
    # 请求 includeThoughts，让 thought 摘要以流式增量进 UI 思考区
    # （与 OpenAI 的 reasoning_content / Anthropic 的 thinking_delta 对齐）。
    if int(thinking.get("thinkingBudget", 1) or 0) == 0:
        thinking.pop("includeThoughts", None)
    else:
        thinking.setdefault("includeThoughts", True)
    return thinking


# =============================================================================
# usage 归一：Gemini usageMetadata -> OpenAI 形状 dict
# =============================================================================
# 目的：让 _log_cache_usage / update_conversation_and_ledger 的既有
# token 台账与缓存命中率观测（Gemini 隐式缓存 cachedContentTokenCount）
# 在原生路径上继续工作，且返回值形状与旧兼容循环一致（dict）。
def _gemini_usage_to_openai(usage_meta) -> Optional[dict]:
    if not isinstance(usage_meta, dict):
        return None

    def _num(value) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return int(value)

    prompt = _num(usage_meta.get("promptTokenCount"))
    candidates = _num(usage_meta.get("candidatesTokenCount"))
    thoughts = _num(usage_meta.get("thoughtsTokenCount"))
    cached = _num(usage_meta.get("cachedContentTokenCount"))
    if not prompt and not candidates and not thoughts:
        return None
    return {
        "prompt_tokens": prompt,
        # 思考 token 属于输出侧计费（与 OpenAI reasoning token 口径一致）。
        "completion_tokens": candidates + thoughts,
        "total_tokens": _num(usage_meta.get("totalTokenCount"))
                        or prompt + candidates + thoughts,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


# =============================================================================
# SSE 流解析：Gemini streamGenerateContent?alt=sse -> 归一化事件
# =============================================================================
# 每个事件是一条 "data: {JSON}"（GenerateContentResponse）。functionCall
# part 为完整对象（无跨 chunk 参数增量）；同响应可有多个 functionCall
# part（并行工具调用）；thought=true 的 text part 是思考摘要；末尾
# chunk 带 finishReason 与 usageMetadata。
async def _iter_gemini_stream_events(resp):
    async for raw_line in resp.content:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            logger.debug("[gemini] 无法解析的 SSE 行（忽略）: %.160s", payload)
            continue
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("usageMetadata"), dict):
            yield {"kind": "usage", "usage": chunk["usageMetadata"]}
        candidates = chunk.get("candidates") or []
        if not candidates:
            continue
        cand = candidates[0] if isinstance(candidates[0], dict) else {}
        finish_reason = str(cand.get("finishReason") or "")
        if finish_reason:
            yield {"kind": "finish", "reason": finish_reason}
        content_obj = cand.get("content")
        if not isinstance(content_obj, dict):
            continue
        for part in (content_obj.get("parts") or []):
            if not isinstance(part, dict):
                continue
            fc = part.get("functionCall")
            if isinstance(fc, dict):
                yield {
                    "kind": "function_call",
                    "name": str(fc.get("name") or ""),
                    "args": fc.get("args") if isinstance(fc.get("args"), dict) else {},
                    "thought_signature": part.get("thoughtSignature") or "",
                }
                continue
            text = part.get("text")
            if not (isinstance(text, str) and text):
                continue
            if part.get("thought"):
                yield {"kind": "thought", "text": text}
            else:
                yield {"kind": "text", "text": text}


def _build_gemini_request_body(
        loop_messages: list,
        *,
        max_tokens: int,
        sampling_params: dict,
        thinking_config: Optional[dict],
        gemini_tools: Optional[list],
) -> dict:
    """构造一次原生 streamGenerateContent 请求体（主流轮与合成总结共用）。"""
    system_instruction, contents = _convert_messages_to_gemini(loop_messages)
    body: dict = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    if sampling_params.get("temperature") is not None:
        body["generationConfig"]["temperature"] = sampling_params["temperature"]
    if sampling_params.get("top_p") is not None:
        body["generationConfig"]["topP"] = sampling_params["top_p"]
    if thinking_config:
        body["generationConfig"]["thinkingConfig"] = thinking_config
    if gemini_tools:
        body["tools"] = gemini_tools
    return body


async def _post_gemini_stream(session: "aiohttp.ClientSession", url: str,
                              headers: dict, body: dict):
    """发起 SSE 流式 POST；非 200 抛 ClientResponseError（带响应体摘要，
    与旧兼容循环的错误路径一致，由上层 get_ai_response 统一格式化）。"""
    resp = await session.post(url, headers=headers, json=body)
    if resp.status not in (200, 201):
        try:
            err_text = await resp.text()
        except Exception:
            err_text = ""
        await resp.release()
        raise aiohttp.ClientResponseError(
            resp.request_info, resp.history,
            status=resp.status,
            message=(err_text or "")[:2000],
        )
    return resp


# =============================================================================
# 原生 agentic 循环（Gemini 原生流式）
# =============================================================================
async def _agentic_loop_gemini_native(
        current_model: str,
        messages: list,
        builder: "RichMessageBuilder",
        tools: list = None,
        supports_tools: bool = True,
        journal: list = None,
) -> tuple[str | None, object | None, list]:
    """Gemini 原生 API 专用循环（streamGenerateContent SSE + 原生 function calling）。

    对外契约与 _agentic_loop_openai_compat / _agentic_loop_anthropic
    完全一致：入参/出参（messages、返回的 new_history_entries）都是
    OpenAI 形状，只在请求 Gemini 原生 API 前后做边界转换（见模块头注释）。
    """
    api_label = "gemini"
    if tools is None:
        from apitelegramchat.search_engine import SEARCH_TOOLS
        tools = SEARCH_TOOLS
    gemini_tools = _convert_tools_to_gemini(tools) if supports_tools else None

    if not GEMINI_API_KEY:
        # 与 api_client 缺 key 时的报错风格一致：显式失败而非 401 黑盒。
        raise ValueError("GEMINI_API_KEY 未设置，无法请求 Gemini 原生 API")

    req_headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    }
    stream_url = (
        f"{_GEMINI_NATIVE_BASE}/models/{current_model}"
        f":streamGenerateContent?alt=sse"
    )

    loop_messages = list(messages)  # OpenAI 形状，供 _run_tool_calls_and_append 复用
    final_content: str | None = None
    final_usage = None
    tool_call_count_ref = [0]
    new_history_entries = journal if journal is not None else []

    model_info = SUPPORTED_MODELS.get(current_model)
    max_tokens = model_info.max_output_tokens if model_info and model_info.max_output_tokens else 8192
    # 采样与推理控制统一来自 config.py（含 per-model 覆盖），禁止在此硬编码。
    sampling_params = get_sampling_params(model_info)
    thinking_config = _gemini_thinking_config(model_info)

    for _round in range(MAX_TOOL_CALLS):
        content_acc = ""
        reasoning_acc = ""
        tool_calls_list: list = []
        # v2.5 语义与 OpenAI 循环对齐：None=尚未见到终止事件；流被完整
        # 消费却仍为 None 且本轮有工具调用时，记 ""（断流证据）。
        finish_reason: Optional[str] = None
        usage_meta: Optional[dict] = None
        received_any = False
        current_stream = None

        def switch_stream(target: str):
            nonlocal current_stream
            if current_stream == target:
                return
            builder.end_stream()
            if target == "reasoning":
                builder.begin_stream_reasoning()
            elif target == "content":
                builder.begin_stream_text()
            current_stream = target

        # 显式缓存（cachedContent API）：可用则只发送“当前回合后缀”并引用
        # 缓存（systemInstruction / 前缀 contents 由缓存提供）；不可用则
        # 照旧全量请求。任何缓存故障都不影响主流程（见 gemini_cache.py）。
        cache_handle = await _gemini_cache_manager.acquire(
            chat_id=builder.chat_id,
            model=current_model,
            messages=loop_messages,
            gemini_tools=gemini_tools,
            convert_fn=_convert_messages_to_gemini,
            base_url=_GEMINI_NATIVE_BASE,
            headers=req_headers,
        )
        if cache_handle is not None:
            request_body = _build_gemini_request_body(
                cache_handle.suffix_messages,
                max_tokens=max_tokens,
                sampling_params=sampling_params,
                thinking_config=thinking_config,
                gemini_tools=gemini_tools,
            )
            request_body["cachedContent"] = cache_handle.name
            logger.info(
                "[%s] 第 %s 轮引用显式缓存 %s（前缀 %s 条消息 + 后缀 %s 条）",
                api_label, _round + 1, cache_handle.name,
                cache_handle.prefix_len, len(cache_handle.suffix_messages),
            )
        else:
            request_body = _build_gemini_request_body(
                loop_messages,
                max_tokens=max_tokens,
                sampling_params=sampling_params,
                thinking_config=thinking_config,
                gemini_tools=gemini_tools,
            )

        try:
            await start_chat_action(builder.chat_id, "typing")
            # typing 状态语义与 OpenAI / Anthropic 循环一致：仅在真实消费
            # 流式增量期间显示（finally 统一熄灭）。
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                try:
                    resp_cm = await _post_gemini_stream(
                        session, stream_url, req_headers, request_body)
                except aiohttp.ClientResponseError as cache_err:
                    if cache_handle is None or cache_err.status not in (400, 404):
                        raise
                    # 缓存引用被拒（过期/不一致/不支持等）：失效该条目并
                    # 降级为全量请求重试一次（自我愈合）。400 发生在流
                    # 开始之前，重试不会产生任何重复输出。
                    logger.warning(
                        "[%s] 第 %s 轮显式缓存被拒（HTTP %s），回退全量请求重试",
                        api_label, _round + 1, cache_err.status)
                    _gemini_cache_manager.invalidate(
                        cache_handle, f"HTTP {cache_err.status}")
                    cache_handle = None
                    request_body = _build_gemini_request_body(
                        loop_messages,
                        max_tokens=max_tokens,
                        sampling_params=sampling_params,
                        thinking_config=thinking_config,
                        gemini_tools=gemini_tools,
                    )
                    resp_cm = await _post_gemini_stream(
                        session, stream_url, req_headers, request_body)
                async with resp_cm as resp:
                    async for event in _iter_gemini_stream_events(resp):
                        received_any = True
                        kind = event["kind"]
                        if kind == "usage":
                            usage_meta = event["usage"]
                            continue
                        if kind == "finish":
                            finish_reason = event["reason"]
                            continue
                        if kind == "function_call":
                            name = event["name"]
                            if not name:
                                logger.warning(
                                    "[%s] 第 %s 轮收到无名 functionCall part，已忽略",
                                    api_label, _round + 1,
                                )
                                continue
                            args = event["args"]
                            # Gemini 原生无 tool call id：合成稳定 id，
                            # 同一 id 同时用于 UI 条目与 tool_calls 条目，
                            # _run_tool_calls_and_append 的 add_tool_item
                            # 会按 id 合并进已显示的条目（不重复建块）。
                            call_id = f"call_{_round}_{len(tool_calls_list)}_{uuid.uuid4().hex[:8]}"
                            tc_entry: dict = {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(
                                        args, ensure_ascii=False,
                                        separators=(",", ":")),
                                },
                            }
                            sig = event.get("thought_signature")
                            if sig:
                                # 双字段存储格式与旧 Gemini 兼容循环一致，
                                # 下一轮边界转换时还原为原生 thoughtSignature。
                                tc_entry["extra_content"] = {
                                    "google": {"thought_signature": sig}
                                }
                                tc_entry["thought_signature"] = sig
                            tool_calls_list.append(tc_entry)
                            summary = _generate_initial_tool_summary(name, args)
                            action_desc = _generate_action_description(name, args)
                            builder.add_tool_item(
                                call_id, name, summary,
                                action_description=action_desc, fn_args=args,
                            )
                            builder.request_flush(force=False)
                            continue
                        if kind == "thought":
                            text = event["text"]
                            reasoning_acc += text
                            switch_stream("reasoning")
                            builder.append_stream_delta(text)
                            continue
                        # kind == "text"
                        text = event["text"]
                        content_acc += text
                        switch_stream("content")
                        builder.append_stream_delta(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"[{api_label}] stream error: {e}")
            raise
        finally:
            await stop_chat_action(builder.chat_id, "typing")

        builder.end_stream()

        # 空流观测（与 OpenAI 循环"流式无有效内容"告警对齐）：HTTP 200
        # 但零 SSE 事件，通常是代理/网关提前断连。
        if not received_any:
            logger.warning(
                "[%s] 第 %s 轮流式响应为空（未收到任何 SSE 事件）",
                api_label, _round + 1,
            )

        # 断流证据（v2.5 语义，与 OpenAI 循环一致）：流被完整消费却从未
        # 见到终止事件，且本轮确实产出了工具调用。
        if finish_reason is None and tool_calls_list:
            finish_reason = ""
        try:
            tool_call_names = [tc["function"]["name"] for tc in tool_calls_list]
            tool_call_ids = [tc["id"] for tc in tool_calls_list]
            logger.info(
                f"[{api_label}] 第 {_round + 1} 轮模型原始返回: tool_calls={len(tool_calls_list)}, "
                f"ids={tool_call_ids}, names={tool_call_names}, content_len={len(content_acc.strip())}, "
                f"reasoning_len={len(reasoning_acc.strip())}, finish_reason={finish_reason!r}"
            )
        except Exception:
            logger.exception(f"[{api_label}] 记录 tool_calls 日志失败")
        # token 台账与缓存命中率观测：usageMetadata 转 OpenAI 形状后
        # 复用既有 _log_cache_usage（Gemini 隐式缓存字段一并呈现）。
        if usage_meta is not None:
            final_usage = _gemini_usage_to_openai(usage_meta)
        _log_cache_usage(api_label, final_usage)

        _normalize_tool_call_arguments(
            tool_calls_list, api_label, _round + 1,
            stream_finish_reason=finish_reason)

        if not tool_calls_list and not content_acc.strip():
            content_acc = "（模型未返回任何内容）"

        # 个别情况下 Gemini 会把函数调用 XML 错当普通正文输出；已在
        # 流式阶段写入草稿，从最终内容里剥离（与旧兼容循环同语义）。
        textual_tool_call = not tool_calls_list and _contains_textual_tool_call(content_acc)
        if textual_tool_call:
            content_acc = _strip_textual_tool_calls(content_acc)

        if reasoning_acc:
            builder.finalize_reasoning_block()
        await builder.flush()

        assistant_msg: dict = {"role": "assistant", "content": content_acc or None}
        if tool_calls_list:
            assistant_msg["tool_calls"] = tool_calls_list
        if reasoning_acc:
            assistant_msg["reasoning_content"] = reasoning_acc
        loop_messages.append(assistant_msg)
        new_history_entries.append(assistant_msg)

        if not tool_calls_list:
            final_content = content_acc
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            # 无工具调用即为终局响应；统一结束旧草稿，不额外开新草稿。
            await builder.rollover_at_turn_boundary(start_next_draft=False)
            break

        status = await _run_tool_calls_and_append(
            tool_calls_list, loop_messages, new_history_entries,
            tool_call_count_ref, api_label, builder, chat_id=builder.chat_id,
            tools=tools,
        )
        # 工具批次后仍会继续请求模型，因此在函数内创建新草稿。
        await builder.rollover_at_turn_boundary(start_next_draft=True)

        if status == "over_limit":
            # 强制总结：与其它两条循环同语义——追加系统指令、禁用工具面、
            # 流式输出最终总结（原生路径下同样实时可见）。
            synth_body = _build_gemini_request_body(
                loop_messages + [{
                    "role": "user",
                    "content": (
                        f"System: Maximum tool calls ({MAX_TOOL_CALLS}) reached for this turn. "
                        "Tool usage is now DISABLED. Please immediately summarize what you have "
                        "successfully done so far, explicitly state what failed or what is left "
                        "to do, and ask the user if they want to continue the operation in the "
                        "next turn."
                    ),
                }],
                max_tokens=max_tokens,
                sampling_params=sampling_params,
                thinking_config=thinking_config,
                gemini_tools=None,
            )
            try:
                await start_chat_action(builder.chat_id, "typing")
                builder.begin_stream_text()
                synth_text = ""
                async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                    async with await _post_gemini_stream(
                            session, stream_url, req_headers, synth_body) as resp:
                        async for event in _iter_gemini_stream_events(resp):
                            if event["kind"] == "text":
                                synth_text += event["text"]
                                builder.append_stream_delta(event["text"])
                raw_synth_content = builder.end_stream_text() or synth_text
                final_content = _strip_textual_tool_calls(raw_synth_content)
                if final_content != raw_synth_content:
                    builder.replace_trailing_text(raw_synth_content, final_content)
                if not final_content:
                    final_content = _tool_limit_summary()
                    builder.add_text(final_content)
            except Exception as synth_err:
                logger.warning(f"[{api_label}] 合成流失败: {synth_err}")
                try:
                    builder.end_stream_text()
                except Exception:
                    logger.debug("_agentic_loop_gemini_native 内部忽略的异常", exc_info=True)
                final_content = _tool_limit_summary()
                builder.add_text(final_content)
            finally:
                await stop_chat_action(builder.chat_id, "typing")
            new_history_entries.append({"role": "assistant", "content": final_content or ""})
            if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
                builder.finish_group(len(builder._tool_groups) - 1)
            # 工具上限总结是终局回复；结束旧草稿，不创建新草稿。
            await builder.rollover_at_turn_boundary(start_next_draft=False)
            break
        # status == "continue"：循环自然继续

    if final_content is None:
        final_content = _tool_limit_summary()
        builder.add_text(final_content)
        new_history_entries.append({"role": "assistant", "content": final_content})
        if builder._tool_groups and not builder._tool_groups[-1].get("finished", False):
            builder.finish_group(len(builder._tool_groups) - 1)
        # 轮次数耗尽后的兜底文本没有后续轮次：结束旧草稿，但不创建新草稿。
        await builder.rollover_at_turn_boundary(start_next_draft=False)

    return final_content, final_usage, new_history_entries
