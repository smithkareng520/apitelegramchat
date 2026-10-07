"""Gemini 原生 API 桥接层，负责 SSE 流式响应与原生 function calling。"""
import asyncio
import copy
import json
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Optional

import aiohttp

from config import (
    GEMINI_API_KEY,
    ModelConfig,
    get_reasoning_request_fields,
)
from utils import get_logger
from chat_actions import start_chat_action, stop_chat_action

from ai._constants import MAX_TOOL_CALLS, STREAM_CLIENT_TIMEOUT, STREAM_READ_BUFSIZE
from ai.errors import AIResponseParseError, AIStreamTimeoutError
from ai.streaming import iter_async_stream
from ai.cache_usage import _log_cache_usage, usage_num
from ai.tool_summary import (
    _contains_textual_tool_call,
    _generate_action_description,
    _generate_initial_tool_summary,
    _normalize_tool_call_arguments,
    _strip_textual_tool_calls,
)
from ai.bridge_common import (
    LiveAssistantSlot,
    append_truncation_notice_if_needed,
    ensure_final_content,
    finish_open_tool_group,
    init_bridge_loop_state,
    make_switch_stream,
    over_limit_final_summary,
    run_tool_batch,
)
from ai.gemini_cache import manager as _gemini_cache_manager

if TYPE_CHECKING:
    from ai.draft_manager import DraftManager

from core.messages import (
    AudioBlock, DocumentBlock, ImageBlock, Message, TextBlock, ToolCallBlock,
    VideoBlock, as_message,
)

logger = get_logger(__name__)

# Gemini 原生 API 基址（v1beta generateContent 协议；非 OpenAI 兼容层）。
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


# 工具 schema 转换：OpenAI function-calling 形状 -> Gemini functionDeclarations
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

    out = {}
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


def _clean_schema_for_gemini(schema: Any, depth: int = 0) -> dict:
    """递归清洗一个 JSON Schema 片段为 Gemini 原生 Schema 子集。

    清洗结果保证 Gemini 两条硬校验不变式在任意层级成立（违反即整请求
    400，线上实测见 _repair_anyof_branch_required docstring）：

    1. ``required`` ⊆ 同层 ``properties``：定义不明的 required 名字剔除，
       空 / 畸形 required 整体移除；
    2. 声明 required / properties 的子 schema 必须带 ``type: "object"``；
    3. ``type: "array"`` 必须带 ``items``（缺失时兜底 ``{"type": "string"}``）。

    另：空 ``properties: {}`` 与不写等价，一并剥掉，避开部分 Gemini
    版本对 OBJECT 空属性的严格校验。

    本函数是**纯函数**：入口深拷贝（仅 depth==0，递归复用已拷贝树），
    绝不修改调用方原始 schema——统一工具面等来源的定义同时被
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
    # 硬校验不变式 3：type=array 必须声明 items（缺失即整请求 400
    # "...items: missing field"）。来源 schema 没写时兜底为字符串元素。
    if out.get("type") == "array" and not isinstance(out.get("items"), dict):
        out["items"] = {"type": "string"}
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


# 消息格式转换：OpenAI 形状 -> Gemini 原生 (systemInstruction, contents)
# 规则（与 anthropic_bridge 同构的边界转换）：
#   - role=system -> 拼接进顶层 systemInstruction（Gemini 无 system 角色）
#   - role=user   -> role=user，content parts 转原生 part
#                    （text / inlineData / fileData）
#   - role=assistant -> role=model；tool_calls 还原为 functionCall part
#                    （args 解析为对象；thoughtSignature 原样回传）
#   - role=tool   -> 攒为下一条 user 消息的 functionResponse part
#                    （Gemini 要求 function response 以 user 角色出现；
#                     连续多条 tool 结果必须合并进同一个 user turn）
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


def _blocks_to_gemini_parts(blocks: list) -> list:
    """把内部内容块列表转换成 Gemini 原生 parts。

    支持的块：TextBlock / ImageBlock（data:base64 内联与 http(s) 公开
    URL）/ VideoBlock / AudioBlock；DocumentBlock 当前模型未开启该能力，
    防御性降级为文本占位；未识别的块退化为文本占位，不中断请求。
    """
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            if block.text:
                parts.append({"text": block.text})
        elif isinstance(block, ImageBlock):
            url = block.url or ""
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
        elif isinstance(block, VideoBlock):
            url = block.url or ""
            if url:
                parts.append({"fileData": {
                    "fileUri": url,
                    "mimeType": _guess_mime_from_url(url, "video/mp4"),
                }})
        elif isinstance(block, AudioBlock):
            data = str(block.data or "")
            fmt = str(block.format or "ogg").lower().lstrip(".")
            if fmt == "oga":
                fmt = "ogg"
            if data:
                parts.append({"inlineData": {
                    "mimeType": f"audio/{fmt}",
                    "data": data,
                }})
        elif isinstance(block, DocumentBlock):
            # 原生文档块仅 Anthropic（document_input=True）启用；
            # Gemini 当前模型未开启该能力，防御性降级为文本占位。
            parts.append({"text": "[收到一个文档附件，当前模型不支持原生文档输入]"})
        else:
            parts.append({"text": f"[不支持的内容类型: {getattr(block, 'kind', lambda: type(block).__name__)()}]"})
    return parts


def _tool_name_for_call_id(messages: list, tool_call_id: str) -> str:
    """从既有消息里回溯 tool_call_id 对应的函数名（functionResponse.name
    必须与 functionCall.name 一致，否则 Gemini 拒绝配对）。"""
    if not tool_call_id:
        return ""
    for raw in messages:
        msg = raw if isinstance(raw, Message) else Message.from_openai_dict(raw)
        for tc in msg.tool_calls():
            if tc.id == tool_call_id:
                return str(tc.name or "")
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


def _thought_signature_for(tc: "ToolCallBlock") -> str:
    """从 ToolCallBlock.extra 提取 thoughtSignature（双存储格式兼容）。

    旧 Gemini 兼容循环把签名写进 wire tool_call 的两个键：
      - thought_signature: "<sig>"
      - extra_content: {"google": {"thought_signature": "<sig>"}}
    Message.assistant_with_tool_calls 会把这两个键原样收进 extra。
    """
    extra = tc.extra or {}
    sig = extra.get("thought_signature")
    if sig:
        return str(sig)
    extra_content = extra.get("extra_content")
    if isinstance(extra_content, dict):
        google = extra_content.get("google")
        if isinstance(google, dict) and google.get("thought_signature"):
            return str(google["thought_signature"])
    return ""


def _convert_messages_to_gemini(messages: list) -> tuple:
    """把内部消息（Message）列表转换成 Gemini 的
    (system_instruction_text, contents)。

    contents 保证：user/model 角色严格交替、至少一条 content、结尾为
    user 或 functionResponse（即下一轮可直接请求 model 回复）。
    """
    system_parts: list = []
    contents: list = []
    pending_function_responses: list = []

    def _flush_function_responses() -> None:
        if pending_function_responses:
            contents.append({"role": "user", "parts": list(pending_function_responses)})
            pending_function_responses.clear()

    for raw in messages:
        msg = as_message(raw)
        role = msg.role
        if role == "system":
            text = msg.text()
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            tr = msg.tool_result_block()
            if tr is None:
                continue
            name = str(tr.name or "") or msg.name or _tool_name_for_call_id(
                messages, tr.tool_call_id)
            text = tr.content if isinstance(tr.content, str) else json.dumps(
                tr.content, ensure_ascii=False)
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
            parts = _blocks_to_gemini_parts(msg.blocks)
            if parts:
                contents.append({"role": "user", "parts": parts})
            continue

        if role == "assistant":
            assistant_parts: list[dict[str, Any]] = []
            text_content = msg.text()
            if text_content:
                assistant_parts.append({"text": text_content})
            for tc in msg.tool_calls():
                call_args = tc.arguments if isinstance(tc.arguments, dict) else {}
                if not isinstance(call_args, dict):
                    call_args = {"value": call_args}
                call_part: dict = {
                    "functionCall": {"name": tc.name, "args": call_args}
                }
                sig = _thought_signature_for(tc)
                if sig:
                    call_part["thoughtSignature"] = sig
                assistant_parts.append(call_part)
            if assistant_parts:
                contents.append({"role": "model", "parts": assistant_parts})
            continue

    _flush_function_responses()

    contents = _merge_consecutive_contents(contents)
    if not contents:
        # 理论上不可达（调用方永远保证至少一条 user 消息）；防御兜底。
        contents = [{"role": "user", "parts": [{"text": "(empty)"}]}]

    system_instruction = "\n\n".join(p for p in system_parts if p)
    return system_instruction, contents


# 推理控制：config 统一出口（get_reasoning_request_fields）-> 原生 thinkingConfig
# config.py 的 gemini 分支产出 OpenAI 兼容层形状（顶层 reasoning_effort +
# extra_body.google.thinking_config.thinkingBudget），本桥接在其上解码为
# 原生 generationConfig.thinkingConfig，保证推理控制仍以 config.py 为
# 单一数据源（不在循环内硬编码预算/档位）：
#   reasoning_effort      -> thinkingLevel（Gemini 3 系档位：low/medium/high）
#   enabled=False         -> thinkingBudget=0（显式关闭思考）
#   reasoning_max_tokens  -> thinkingBudget
#   其余                  -> 不发送 thinkingConfig（跟随供应商默认）
def _gemini_thinking_config(model_info: Optional[ModelConfig]) -> Optional[dict]:
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


# usage 归一：Gemini usageMetadata -> OpenAI 形状 dict
# 目的：让 _log_cache_usage / update_conversation_and_ledger 的既有
# token 台账与缓存命中率观测（Gemini 隐式缓存 cachedContentTokenCount）
# 在原生路径上继续工作，且返回值形状与旧兼容循环一致（dict）。
def _gemini_usage_to_openai(usage_meta: Any) -> Optional[dict]:
    if not isinstance(usage_meta, dict):
        return None

    prompt = usage_num(usage_meta.get("promptTokenCount"))
    candidates = usage_num(usage_meta.get("candidatesTokenCount"))
    thoughts = usage_num(usage_meta.get("thoughtsTokenCount"))
    cached = usage_num(usage_meta.get("cachedContentTokenCount"))
    if not prompt and not candidates and not thoughts:
        return None
    return {
        "prompt_tokens": prompt,
        # 思考 token 属于输出侧计费（与 OpenAI reasoning token 口径一致）。
        "completion_tokens": candidates + thoughts,
        "total_tokens": usage_num(usage_meta.get("totalTokenCount"))
                        or prompt + candidates + thoughts,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


# SSE 流解析：Gemini streamGenerateContent?alt=sse -> 归一化事件
# 每个事件是一条 "data: {JSON}"（GenerateContentResponse）。functionCall
# part 为完整对象（无跨 chunk 参数增量）；同响应可有多个 functionCall
# part（并行工具调用）；thought=true 的 text part 是思考摘要；末尾
# chunk 带 finishReason 与 usageMetadata。
def _gemini_chunk_to_events(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    """把一个 GenerateContentResponse chunk 归一化为零或多个内部事件。

    流中 error 事件与安全拦截也在这里上抛为 error 事件，不能静默吞掉：
    两者都表现为"无 candidates 的 chunk"，直接丢弃会让调用方只看到空流
    （零事件），根因完全不可观测，也没有任何重试机会。
    """
    events: list[dict[str, Any]] = []
    #   - error chunk：{"error": {"code": 503, "message": ..., "status": ...}}
    #   - 安全拦截：{"promptFeedback": {"blockReason": "SAFETY", ...}}
    error = chunk.get("error")
    if isinstance(error, dict):
        status = error.get("status") or error.get("code") or ""
        message = str(error.get("message") or error)
        events.append({"kind": "error", "message": message, "status": str(status)})
        return events
    feedback = chunk.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        events.append({
            "kind": "error",
            "message": (f"prompt blocked by safety filters: "
                        f"{feedback.get('blockReason')}"),
            "status": "SAFETY_BLOCK",
            "safety_block": True,
        })
        return events
    if isinstance(chunk.get("usageMetadata"), dict):
        events.append({"kind": "usage", "usage": chunk["usageMetadata"]})
    candidates = chunk.get("candidates") or []
    if not candidates:
        return events
    cand = candidates[0] if isinstance(candidates[0], dict) else {}
    finish_reason = str(cand.get("finishReason") or "")
    if finish_reason:
        events.append({"kind": "finish", "reason": finish_reason})
    content_obj = cand.get("content")
    if not isinstance(content_obj, dict):
        return events
    for part in (content_obj.get("parts") or []):
        if not isinstance(part, dict):
            continue
        fc = part.get("functionCall")
        if isinstance(fc, dict):
            events.append({
                "kind": "function_call",
                "name": str(fc.get("name") or ""),
                "args": fc.get("args") if isinstance(fc.get("args"), dict) else {},
                "thought_signature": part.get("thoughtSignature") or "",
            })
            continue
        text = part.get("text")
        if not (isinstance(text, str) and text):
            continue
        if part.get("thought"):
            events.append({"kind": "thought", "text": text})
        else:
            events.append({"kind": "text", "text": text})
    return events


def _parse_sse_data_payload(payload: str) -> list[dict[str, Any]]:
    """解析一条 SSE 事件的 data 载荷（可能是多行拼接后的整体 JSON）。"""
    if not payload or payload == "[DONE]":
        return []
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError:
        # warning 而非 debug：正常网关不会发出非法 data 载荷，出现即说明
        # 网关/代理行为异常，静默忽略会让问题只表现为"零事件空流"。
        logger.warning("[gemini] 无法解析的 SSE 载荷（忽略）: %.160s", payload)
        return []
    if not isinstance(chunk, dict):
        return []
    return _gemini_chunk_to_events(chunk)


async def _iter_gemini_stream_events(resp: aiohttp.ClientResponse) -> AsyncIterator[dict[str, Any]]:
    """按 SSE 规范切分事件：空行为事件边界，同一事件的多行 data 字段
    以 \n 连接后整体解析（Gemini 官方是单行 data，网关/代理可能拆行）。

    每段到达的字节自行按 \n 再切分，不依赖底层 readline 的行交付粒度：
    无论传输层逐行交付还是一次交付多行（含 \r\n 行尾），事件边界判定
    都保持一致。
    """
    data_lines: list[str] = []
    async for raw in iter_async_stream(resp.content):
        for sse_line in raw.decode("utf-8", errors="replace").split("\n"):
            line = sse_line.strip()
            if not line:
                if data_lines:
                    for event in _parse_sse_data_payload("\n".join(data_lines)):
                        yield event
                    data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
            # event:/id:/注释行与 Gemini 载荷无关，忽略。
    # 流结束但最后一个事件没有空行收尾：缓冲中的事件不能丢。
    if data_lines:
        for event in _parse_sse_data_payload("\n".join(data_lines)):
            yield event


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
                              headers: dict, body: dict) -> aiohttp.ClientResponse:
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


# 原生 agentic 循环（Gemini 原生流式）
async def _agentic_loop_gemini_native(
        current_model: str,
        messages: list,
        builder: "DraftManager",
        tools: list | None = None,
        supports_tools: bool = True,
        journal: list | None = None,
        workspace_namespace: str | None = None,
) -> tuple[str | None, object | None, list]:
    """Gemini 原生 API 专用循环（streamGenerateContent SSE + 原生 function calling）。

    对外契约与 _agentic_loop_openai_compat / _agentic_loop_anthropic
    完全一致：入参/出参（messages、返回的 new_history_entries）统一为
    内部 Message（core/messages），只在请求 Gemini 原生 API 前做内部 ->
    原生协议的边界转换（见模块头注释）。
    """
    api_label = "gemini"
    if tools is None:
        from tool_registry import get_model_tools
        tools = await get_model_tools()
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

    state = init_bridge_loop_state(messages, journal, current_model)
    loop_messages = state.loop_messages
    new_history_entries = state.new_history_entries
    tool_call_count_ref = state.tool_call_count_ref
    final_content: str | None = state.final_content
    final_usage = state.final_usage
    model_info = state.model_info
    max_tokens = state.max_tokens
    sampling_params = state.sampling_params
    thinking_config = _gemini_thinking_config(model_info)

    for _round in range(MAX_TOOL_CALLS):
        from skills_runtime import refresh_skill_catalog as _refresh_skill_catalog
        _refresh_skill_catalog(loop_messages, builder, workspace_namespace)
        content_acc = ""
        reasoning_acc = ""
        tool_calls_list: list = []
        # 流式期间 journal 始终持有一条与 content_acc / reasoning_acc 同步的
        # assistant 占位消息。Gemini 的 functionCall 事件虽携带完整参数（无
        # 半截 JSON 问题），但按四循环统一约束，tool_calls 仍只在流正常结束
        live_slot = LiveAssistantSlot(new_history_entries)
        # 消费却仍为 None 且本轮有工具调用时，记 ""（断流证据）。
        finish_reason: Optional[str] = None
        usage_meta: Optional[dict] = None
        received_any = False
        current_stream_cell = [None]

        switch_stream = make_switch_stream(builder, current_stream_cell)

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
            async with aiohttp.ClientSession(timeout=STREAM_CLIENT_TIMEOUT, read_bufsize=STREAM_READ_BUFSIZE) as session:
                # 零输出瞬态重试（与 openai_compat 循环首增量前读超时重试
                # 同语义）：请求建立 / 流开始前的连接类瞬态失败（代理抖动、
                # 网关 5xx、连接重置）且尚未收到任何事件时重试一次；已有
                # 增量后绝不重放半个回合。流中 error / 安全拦截事件则直接
                # 上抛（见下方 kind == "error" 分支）——安全拦截是确定性
                # 结果，重试无意义。
                for stream_attempt in range(2):
                    try:
                        resp_cm = await _post_gemini_stream(
                            session, stream_url, req_headers, request_body)
                    except aiohttp.ClientResponseError as cache_err:
                        if cache_handle is None or cache_err.status not in (400, 404):
                            # 非「缓存被拒」类错误：留给下方瞬态重试分支
                            # 判定（零输出阶段可重试一次），否则原样上抛。
                            if received_any or stream_attempt >= 1:
                                raise
                            logger.warning(
                                "[%s] 第 %s 轮流式请求在首个事件前瞬态失败（HTTP %s），1s 后重试一次",
                                api_label, _round + 1, cache_err.status,
                            )
                            await asyncio.sleep(1.0)
                            continue
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
                    try:
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
                                if kind == "error":
                                    logger.error(
                                        "[%s] 第 %s 轮流中错误事件（status=%s）： %s",
                                        api_label, _round + 1,
                                        event.get("status", ""), event["message"],
                                    )
                                    raise AIResponseParseError(
                                        f"[gemini] 流式响应中报告错误"
                                        f"（status={event.get('status', '') or 'unknown'}）: "
                                        f"{event['message']}"
                                    )
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
                                    await switch_stream("reasoning")
                                    builder.append_stream_delta(text)
                                    live_slot.sync(content_acc, reasoning_acc)
                                    continue
                                # kind == "text"
                                text = event["text"]
                                content_acc += text
                                await switch_stream("content")
                                builder.append_stream_delta(text)
                                live_slot.sync(content_acc, reasoning_acc)
                        break  # 流被完整消费：成功，退出零输出重试循环
                    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as e:
                        # 连接类瞬态失败（ClientError 覆盖 ClientResponseError /
                        # ServerDisconnectedError 等）：仅零输出阶段补试一次；
                        # AIResponseParseError（流中 error/安全拦截）不是连接
                        # 类异常，直接落到外层上抛，绝不重试。
                        # 应用层 total 期限是硬闸门，同样绝不重试（重试只会
                        # 重放同样漫长的等待）；idle 期限按瞬态策略重试。
                        if isinstance(e, AIStreamTimeoutError) and e.kind == "total":
                            raise
                        if received_any or stream_attempt >= 1:
                            raise
                        logger.warning(
                            "[%s] 第 %s 轮流式请求在首个事件前瞬态失败（%s: %.200s），1s 后重试一次",
                            api_label, _round + 1, type(e).__name__, str(e),
                        )
                        await asyncio.sleep(1.0)
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
        _log_cache_usage(api_label, final_usage, model_name=current_model)

        _normalize_tool_call_arguments(
            tool_calls_list, api_label, _round + 1,
            stream_finish_reason=finish_reason)

        if not tool_calls_list and not content_acc.strip():
            content_acc = "（模型未返回任何内容）"

        # 个别情况下 Gemini 会把函数调用 XML 错当普通正文输出；该内容
        # 已在流式阶段写入草稿，必须先从构建器撤回，再从最终内容里剥离
        # （与 _agentic_loop_openai_compat 同语义）。
        textual_tool_call = not tool_calls_list and _contains_textual_tool_call(content_acc)
        if textual_tool_call:
            raw_textual_content = content_acc
            content_acc = _strip_textual_tool_calls(content_acc)
            if not builder.replace_trailing_text(raw_textual_content, content_acc):
                logger.warning(
                    "[%s] 未能在草稿中定位伪工具调用文本，已阻止其进入最终内容",
                    api_label,
                )

        if reasoning_acc:
            builder.finalize_reasoning_block()

        # 块边界换草稿检查点①②（本轮最后一个块）：流已结束，最后一个思考块
        # 或文本块在此闭合，switch_stream 不会再被触发，故在此补一次检查。
        # _has_pending_tool_group 守卫兜住：本轮若已建工具条目而未收束，
        # 这里不会滚动，工具批次结束后的 tool.end 安全点仍会照常触发。
        # 终局轮修复：此处 tool_calls_list 已定型。本循环对伪工具调用文本
        # 只做剥离、不重试（剥离后即终局，见下方 not tool_calls_list 分支），
        # 故无需把 textual_tool_call 计入继续条件。解耦改造：中途安全点
        # （工具批次待执行）只发射事件，满容量时由 DraftManager 在后台
        # 滚动，Agent 立即继续（§8）；纯文本终局轮必须走 finalize_turn
        # 同步收束，把"只永久化旧段、不创建新草稿"留给终局分支完成。
        # 否则容量预警标志会在终局轮被本检查点以 start_next_draft=True
        # 抢先消费——创建一个永远无人写入、只显示 "Thinking..." 的幽灵
        # 草稿，随后被 get_ai_response 收尾 mark_dead + 删除（表现为回复
        # 交付后闪现的 Thinking 气泡）。
        will_request_again = bool(tool_calls_list)
        if will_request_again:
            # 非阻塞安全点：满容量时后台滚动；工具批次立即开始。
            builder.on_round_boundary()
            builder.request_flush()
        elif not await builder.finalize_turn():
            # 终局：等待旧段永久化（不开新草稿）；未滚动时保底刷一帧。
            builder.request_flush()

        # 纯文本终局截断提示：必须在 live_slot.finalize 之前算出追加后的
        # 文本，否则 journal/loop_messages 定稿的仍是未追加提示的
        # content_acc（与 anthropic_bridge 同一修复，理由见 bridge_common）。
        if not tool_calls_list:
            content_acc = append_truncation_notice_if_needed(
                builder, content_acc, finish_reason)

        # （tool_calls / reasoning / 最终文本原地补全，同一对象进 loop_messages）。
        live_slot.finalize(loop_messages, content_acc, tool_calls_list, reasoning_acc)

        if not tool_calls_list:
            final_content = content_acc
            finish_open_tool_group(builder)
            # 无工具调用即为终局响应；同步结束旧草稿，不额外开新草稿。
            await builder.finalize_turn()
            break

        status = await run_tool_batch(builder, tool_calls_list, loop_messages,
                                      new_history_entries, tool_call_count_ref,
                                      api_label, tools, error_streak=state.error_streak)

        if status == "over_limit":

            async def _synth_stream(synth_body: dict) -> str:
                synth_text = ""
                async with aiohttp.ClientSession(timeout=STREAM_CLIENT_TIMEOUT, read_bufsize=STREAM_READ_BUFSIZE) as session:
                    async with await _post_gemini_stream(
                            session, stream_url, req_headers, synth_body) as resp:
                        async for event in _iter_gemini_stream_events(resp):
                            if event["kind"] == "text":
                                synth_text += event["text"]
                                builder.append_stream_delta(event["text"])
                return synth_text

            final_content = await over_limit_final_summary(
                builder, new_history_entries,
                api_label=api_label, loop_name="_agentic_loop_gemini_native",
                build_synth_request=lambda extra: _build_gemini_request_body(
                    loop_messages + [extra],
                    max_tokens=max_tokens,
                    sampling_params=sampling_params,
                    thinking_config=thinking_config,
                    gemini_tools=None,
                ),
                stream_synth=_synth_stream,
                postprocess=_strip_textual_tool_calls,
            )
            break
        # status == "continue"：循环自然继续

    final_content = await ensure_final_content(builder, new_history_entries, final_content)

    return final_content, final_usage, new_history_entries
