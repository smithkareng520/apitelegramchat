# -*- coding: utf-8 -*-
"""内部消息模型（Internal Message）：与具体 API 协议解耦的消息表示。

设计动机（协议层重构第二阶段）
==============================

旧架构把 OpenAI Chat Completions 的 JSON dict（role/content/tool_calls…）
当作全链路的内部格式：历史存储、上下文管理、三个 agentic 循环、两个
原生桥接全部直接读写 OpenAI 形状的 dict。后果：

  1. "内部格式 = 某一家厂商的线上格式"，Anthropic / Gemini 桥接被迫做
     "OpenAI dict -> 原生 JSON" 的两跳转换，语义淹没在字段搬运里；
  2. 多模态内容（图片/音频/视频/文档）与 Telegram 侧附件元数据
     （file_id / attachments）混在同一个 dict 里，出站字段清洗靠约定。

本模块定义协议无关的内部消息：

    Message(role, blocks, name=None, meta=None)

  - blocks: 内容块列表（TextBlock / ImageBlock / AudioBlock / VideoBlock /
    DocumentBlock / ToolCallBlock / ToolResultBlock / ReasoningBlock），
    多模态是一等公民；
  - meta:   Telegram 侧附件元数据与内部标记（TURN_FAILED_FLAG 等），
    **永不渲染进出站请求**（替代旧版"出站前手工剔除内部字段"的约定）。

互操作（兼容与出站）
====================

  - to_openai_dict():   渲染为 OpenAI Chat Completions JSON（兼容层唯一
                        出口：openai_chat 循环、subagent 兼容请求）；
  - from_openai_dict(): 从 OpenAI JSON（或含附件元数据的混合存储形状）
                        还原 Message（历史/调用方兼容路径）。

协议适配器（protocols/*）各自把 Message 渲染为原生协议 JSON：
  - openai_chat        -> to_openai_dict()
  - anthropic_messages -> ai/anthropic_bridge._convert_messages_to_anthropic
  - gemini_native      -> ai/gemini_bridge._convert_messages_to_gemini

与旧 dict 形状的关系
====================

历史存储（state.conversation_history）、journal、tool 回写从此统一持有
Message 对象。context_window / context_manager 等纯逻辑模块同时接受
Message 与旧 dict（双形状适配），旧测试与调用方零成本迁移。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


# ===========================================================================
# 内容块（Blocks）
# ===========================================================================
@dataclass
class TextBlock:
    """纯文本内容。"""
    text: str = ""

    def kind(self) -> str:
        return "text"


@dataclass
class ReasoningBlock:
    """思考/推理内容（assistant 消息；OpenAI 侧渲染为 reasoning_content）。"""
    text: str = ""

    def kind(self) -> str:
        return "reasoning"


@dataclass
class ImageBlock:
    """图片输入。

    url 同时接受 http(s) 公开 URL 与 data:image/...;base64,... 内联——
    由各协议适配器决定内联/引用的表达方式（Anthropic base64 source /
    Gemini inlineData / OpenAI image_url）。
    """
    url: str = ""
    detail: Optional[str] = None  # OpenAI image_url.detail（auto/low/high）

    def kind(self) -> str:
        return "image"


@dataclass
class AudioBlock:
    """音频输入（OpenAI input_audio 形状：纯 base64 数据 + 格式）。"""
    data: str = ""       # base64（不含 data: 前缀）
    format: str = "ogg"  # wav / mp3 / ogg ...

    def kind(self) -> str:
        return "audio"


@dataclass
class VideoBlock:
    """视频输入（事实标准 video_url content part，统一公开 URL）。"""
    url: str = ""

    def kind(self) -> str:
        return "video"


@dataclass
class DocumentBlock:
    """文档输入（PDF 为主）。

    - url:      公开 URL（Anthropic url source 直通，服务端自行抓取）；
    - data_url: data:application/pdf;base64,... 内联（OpenAI file part /
      Anthropic base64 source）。
    二者至少提供一个；Anthropic 以外的协议当前降级为文本占位。
    """
    url: str = ""
    data_url: str = ""
    filename: str = ""
    mime: str = "application/pdf"

    def kind(self) -> str:
        return "document"


@dataclass
class ToolCallBlock:
    """assistant 发起的工具调用。

    arguments 为解析后的 dict（构造时由 JSON 字符串解析；渲染回
    OpenAI wire 时再 json.dumps，保证历史里存的是结构化数据而非
    半截 JSON 字符串）。
    """
    id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)
    # 协议特有元数据（如 Gemini thoughtSignature）：渲染 OpenAI wire 时
    # 以原样键值并入该 tool_call 的 JSON（供桥接往返）。
    extra: dict = field(default_factory=dict)

    def kind(self) -> str:
        return "tool_call"


@dataclass
class ToolResultBlock:
    """工具执行结果（role=tool 消息的唯一内容块）。"""
    tool_call_id: str = ""
    name: str = ""
    content: str = ""
    is_error: bool = False

    def kind(self) -> str:
        return "tool_result"


Block = (
    TextBlock | ReasoningBlock | ImageBlock | AudioBlock
    | VideoBlock | DocumentBlock | ToolCallBlock | ToolResultBlock
)


def _parse_arguments(raw: Any) -> dict:
    """工具调用参数容错解析：dict 直通，JSON 字符串解析，失败回 {}。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


# ===========================================================================
# Message
# ===========================================================================
@dataclass
class Message:
    """协议无关的内部消息。

    role: system / user / assistant / tool
    blocks: 内容块列表（见模块 docstring）
    name: 可选的 tool 消息函数名（OpenAI 兼容字段；也用于 Gemini
          functionResponse 配对回溯的冗余记录）
    meta: Telegram 侧附件元数据 + 内部标记。永不进出站请求体；
          持久历史携带它，让下一轮仍能按新模型能力重新解析附件
          （见 attachment_content._append_history_async）。
    """

    role: str
    blocks: list[Block] = field(default_factory=list)
    name: Optional[str] = None
    meta: dict = field(default_factory=dict)

    # ---------- 便捷构造器 ----------
    @classmethod
    def system(cls, text: str, **meta: Any) -> "Message":
        return cls(role="system", blocks=[TextBlock(text)], meta=dict(meta))

    @classmethod
    def user(cls, blocks: list[Block], **meta: Any) -> "Message":
        return cls(role="user", blocks=list(blocks or []), meta=dict(meta))

    @classmethod
    def user_text(cls, text: str, **meta: Any) -> "Message":
        return cls(role="user", blocks=[TextBlock(text)], meta=dict(meta))

    @classmethod
    def assistant_text(cls, text: str = "", reasoning: str = "") -> "Message":
        blocks: list[Block] = []
        if reasoning:
            blocks.append(ReasoningBlock(reasoning))
        if text:
            blocks.append(TextBlock(text))
        return cls(role="assistant", blocks=blocks)

    @classmethod
    def assistant_with_tool_calls(
        cls,
        content: Optional[str],
        tool_calls: list,
        reasoning: str = "",
    ) -> "Message":
        """由 OpenAI wire 形状的 tool_calls 列表（流式累积产物）构造。

        tool_calls: [{"id","type","function":{"name","arguments"}}...]，
        arguments 接受 JSON 字符串（解析为 dict 存储）；id/type/function
        之外的键（如 Gemini thought_signature）收进 ToolCallBlock.extra，
        渲染回 wire 时原样并入。
        """
        blocks: list[Block] = []
        if reasoning:
            blocks.append(ReasoningBlock(reasoning))
        if content:
            blocks.append(TextBlock(content))
        for tc in (tool_calls or []):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            extra = {
                k: v for k, v in tc.items()
                if isinstance(tc, dict) and k not in ("id", "type", "function")
            }
            blocks.append(ToolCallBlock(
                id=str(tc.get("id") or "") if isinstance(tc, dict) else "",
                name=str(fn.get("name") or ""),
                arguments=_parse_arguments(fn.get("arguments")),
                extra=extra,
            ))
        return cls(role="assistant", blocks=blocks)

    @classmethod
    def tool_result(cls, tool_call_id: str, name: str, content: str) -> "Message":
        return cls(
            role="tool",
            blocks=[ToolResultBlock(tool_call_id=tool_call_id, name=name, content=str(content or ""))],
            name=name or None,
        )

    # ---------- 内容访问辅助 ----------
    def text(self) -> str:
        """全部 TextBlock 拼接（tool 角色返回 ToolResultBlock.content）。"""
        if self.role == "tool":
            for b in self.blocks:
                if isinstance(b, ToolResultBlock):
                    return b.content
            return ""
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock) and b.text)

    def reasoning(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, ReasoningBlock) and b.text)

    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.blocks if isinstance(b, ToolCallBlock)]

    def tool_result_block(self) -> Optional[ToolResultBlock]:
        for b in self.blocks:
            if isinstance(b, ToolResultBlock):
                return b
        return None

    def images(self) -> list[ImageBlock]:
        return [b for b in self.blocks if isinstance(b, ImageBlock)]

    def set_text(self, text: str) -> None:
        """整体替换文本内容（保留非文本块）；用于裁剪/归档改写。"""
        kept = [b for b in self.blocks if not isinstance(b, TextBlock)]
        self.blocks = ([TextBlock(text)] if text else []) + kept

    def append_text(self, text: str) -> None:
        if not text:
            return
        for b in reversed(self.blocks):
            if isinstance(b, TextBlock):
                b.text += text
                return
        self.blocks.append(TextBlock(text))

    # ---------- OpenAI Chat Completions 互转 ----------
    def to_openai_dict(self) -> dict[str, Any]:
        """渲染为 OpenAI Chat Completions JSON（出站唯一出口）。

        meta 永不出站；system/user/assistant 的多模态块渲染为 content
        parts 列表，纯文本退化为字符串（与旧线上形状逐字节兼容，保住
        前缀缓存）。
        """
        out: dict[str, Any] = {"role": self.role}
        if self.role == "tool":
            tr = self.tool_result_block()
            out["tool_call_id"] = tr.tool_call_id if tr else ""
            out["content"] = tr.content if tr else ""
            if self.name or (tr and tr.name):
                out["name"] = self.name or tr.name  # type: ignore[union-attr]
            return out

        if self.role == "assistant":
            text = self.text()
            out["content"] = text if text else None
            calls = self.tool_calls()
            if calls:
                out["tool_calls"] = [self._tool_call_to_wire(tc) for tc in calls]
            reasoning = self.reasoning()
            if reasoning:
                out["reasoning_content"] = reasoning
            return out

        # system / user：单文本块 -> 字符串；否则 content parts 列表。
        parts = [self._block_to_wire_part(b) for b in self.blocks]
        parts = [p for p in parts if p is not None]
        if len(parts) == 1 and parts[0].get("type") == "text":
            out["content"] = parts[0]["text"]
        elif parts:
            out["content"] = parts
        else:
            out["content"] = ""
        return out

    @staticmethod
    def _tool_call_to_wire(tc: ToolCallBlock) -> dict[str, Any]:
        try:
            args_json = json.dumps(tc.arguments or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            args_json = "{}"
        wire: dict[str, Any] = {
            "id": tc.id,
            "type": "function",
            "function": {"name": tc.name, "arguments": args_json},
        }
        # 协议特有元数据（thoughtSignature 等）原样并入。
        for k, v in (tc.extra or {}).items():
            if k not in wire:
                wire[k] = v
        return wire

    @staticmethod
    def _block_to_wire_part(b: Block) -> Optional[dict[str, Any]]:
        if isinstance(b, TextBlock):
            return {"type": "text", "text": b.text} if b.text else None
        if isinstance(b, ImageBlock):
            part: dict[str, Any] = {"type": "image_url", "image_url": {"url": b.url}}
            if b.detail:
                part["image_url"]["detail"] = b.detail
            return part
        if isinstance(b, AudioBlock):
            return {"type": "input_audio", "input_audio": {"data": b.data, "format": b.format}}
        if isinstance(b, VideoBlock):
            return {"type": "video_url", "video_url": {"url": b.url}}
        if isinstance(b, DocumentBlock):
            if b.data_url:
                return {"type": "file", "file": {"filename": b.filename, "file_data": b.data_url}}
            if b.url:
                # 公开 URL 文档：OpenAI 兼容层没有标准形状，沿用 file_data
                # 直传 URL（网关不支持时由 attachment 层早已降级为文本）。
                return {"type": "file", "file": {"filename": b.filename, "file_data": b.url}}
            return None
        if isinstance(b, ReasoningBlock):
            return None  # assistant 侧单独渲染 reasoning_content
        if isinstance(b, (ToolCallBlock, ToolResultBlock)):
            return None  # 不属于 system/user 消息
        return None

    @classmethod
    def from_openai_dict(cls, msg: dict[str, Any]) -> "Message":
        """从 OpenAI JSON（或混合存储形状）还原 Message。

        兼容两种输入：
          - 纯 wire 形状（role/content/tool_calls/reasoning_content/...）
          - 历史混合形状（额外携带 type/file_id/file_ids/attachments 等
            Telegram 侧元数据）——全部归入 meta，绝不进出站渲染。
        """
        role = str(msg.get("role") or "user")
        meta = {
            k: v for k, v in msg.items()
            if k not in ("role", "content", "tool_calls", "tool_call_id",
                         "name", "reasoning_content")
        }

        blocks: list[Block] = []
        if role == "assistant":
            reasoning = msg.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                blocks.append(ReasoningBlock(reasoning))
            content = msg.get("content")
            if isinstance(content, str) and content:
                blocks.append(TextBlock(content))
            elif isinstance(content, list):
                blocks.extend(cls._parts_to_blocks(content))
            for tc in (msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                extra = {
                    k: v for k, v in tc.items()
                    if k not in ("id", "type", "function")
                }
                blocks.append(ToolCallBlock(
                    id=str(tc.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    arguments=_parse_arguments(fn.get("arguments")),
                    extra=extra,
                ))
            return cls(role="assistant", blocks=blocks)

        if role == "tool":
            content = msg.get("content", "")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            name = msg.get("name")
            blocks.append(ToolResultBlock(
                tool_call_id=str(msg.get("tool_call_id") or ""),
                name=str(name or ""),
                content=text,
            ))
            return cls(role="tool", blocks=blocks, name=(str(name) if name else None))

        # system / user
        content = msg.get("content")
        if isinstance(content, str):
            if content:
                blocks.append(TextBlock(content))
        elif isinstance(content, list):
            blocks.extend(cls._parts_to_blocks(content))
        return cls(role=role, blocks=blocks, meta=meta)

    @staticmethod
    def _parts_to_blocks(parts: list) -> list[Block]:
        """OpenAI content parts -> 内部块（未识别类型跳过，与旧语义一致）。"""
        out: list[Block] = []
        for part in parts:
            if isinstance(part, str):
                if part:
                    out.append(TextBlock(part))
                continue
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                text = str(part.get("text") or "")
                if text:
                    out.append(TextBlock(text))
            elif ptype == "image_url":
                url_obj = part.get("image_url") or {}
                url = url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj)
                if url:
                    out.append(ImageBlock(url=url, detail=url_obj.get("detail") if isinstance(url_obj, dict) else None))
            elif ptype == "input_audio":
                audio = part.get("input_audio") or {}
                if audio.get("data"):
                    out.append(AudioBlock(data=str(audio.get("data")), format=str(audio.get("format") or "ogg")))
            elif ptype == "video_url":
                url_obj = part.get("video_url") or {}
                url = url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj)
                if url:
                    out.append(VideoBlock(url=url))
            elif ptype == "file":
                fobj = part.get("file") or {}
                file_data = str(fobj.get("file_data") or "")
                if file_data:
                    out.append(DocumentBlock(
                        data_url=file_data if file_data.startswith("data:") else "",
                        url=("" if file_data.startswith("data:") else file_data),
                        filename=str(fobj.get("filename") or ""),
                    ))
            elif ptype == "document":
                # Anthropic 原生形状直通块（历史兼容）：还原为 DocumentBlock
                source = part.get("source") or {}
                if isinstance(source, dict) and source.get("type") == "url":
                    out.append(DocumentBlock(url=str(source.get("url") or ""), filename=str(part.get("title") or "")))
                elif isinstance(source, dict) and source.get("type") == "base64":
                    header = str(source.get("media_type") or "application/pdf")
                    out.append(DocumentBlock(data_url=f"data:{header};base64,{source.get('data', '')}"))
            # 其它类型（audio/未知）跳过，与旧转换语义一致
        return out

    # ---------- 持久化快照 ----------
    def to_snapshot(self) -> "Message":
        """返回用于请求的浅快照（同对象引用，无拷贝——历史对象不可变约定）。"""
        return self

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        kinds = ",".join(b.kind() for b in self.blocks)
        return f"<Message {self.role} [{kinds}] meta={list(self.meta)}>"


# ===========================================================================
# 列表级辅助
# ===========================================================================
def render_openai_messages(messages: list) -> list[dict[str, Any]]:
    """把 Message 列表渲染为 OpenAI wire dict 列表（兼容旧 dict 直通）。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, Message):
            out.append(m.to_openai_dict())
        else:
            out.append(m)  # 旧 dict 直通（双形状过渡期兼容）
    return out


def as_message(msg: Any) -> Message:
    """旧 dict -> Message（Message 原样返回）——双形状适配入口。"""
    if isinstance(msg, Message):
        return msg
    if isinstance(msg, dict):
        return Message.from_openai_dict(msg)
    raise TypeError(f"无法解释的消息类型: {type(msg)!r}")


def first_text(messages: list, role: str, *, from_end: bool = False) -> str:
    """取列表中（默认第一条 / from_end 时最后一条）指定角色的文本。"""
    seq: Iterator = reversed(messages) if from_end else iter(messages)
    for m in seq:
        m = as_message(m)
        if m.role == role:
            return m.text()
    return ""


__all__ = [
    "TextBlock", "ReasoningBlock", "ImageBlock", "AudioBlock", "VideoBlock",
    "DocumentBlock", "ToolCallBlock", "ToolResultBlock", "Block",
    "Message", "render_openai_messages", "as_message", "first_text",
]
