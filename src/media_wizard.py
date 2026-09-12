# -*- coding: utf-8 -*-
"""媒体生成参数交互卡片（wizard）。

触发与流程
--------
用户对图像/视频生成模型发送 prompt（USER 回合，统一管道预检 route=image/video
且鉴权通过）→ 不直接生成，而是发出一张"参数卡片"；用户在卡片上翻页配置参数
（editMessageText 就地编辑同一条消息，前进/后退导航）、按需补传参考媒体
（首尾帧 / 参考图 / 参考音频 / 参考视频），最后点"✅ 开始生成"提交——实际生成
以 turn 任务驱动媒体循环，参数经 media_overrides 注入请求层。

按钮来源（数据驱动，"根据模型的参数来判断富文本交互按钮"）
--------
卡片按钮完全由模型有效参数推导（resolve_request_plan 的 api_type + 图像形状 +
Agnes 官方文档参数表）：请求层不消费 / API 不接受的参数绝不出现——
  - Agnes Image 2.5 Flash（inline 形状）：size 档位（1K–4K）、ratio（官方 8
    比例）、参考图片（图生图 / 多图合成，extra_body.image）；
  - Agnes Video 2.5：mode（text/keyframe/reference）、seconds（"4"–"12"）、
    size（720P/1080P/1K/2K）、aspect_ratio（官方 6 比例）、seed、首尾帧
    （keyframe）、参考图(≤8)/音频(≤3)/视频(≤1，含 start_seconds / require_audio)；
    n 模型固定为 1 → 只作说明文字，不提供按钮（文档：n 传非 1 会 400）；
  - 其他图像模型（multipart / chat modalities 形状）：请求层不消费
    size/ratio → 只提供提示词与提交。

默认与参考媒体
--------
未选择的参数不进请求体（跟随 API 网关默认，与官方文档一致）；参考媒体统一
经 R2 转为公开可访问 URL（与图片输入的 R2 公开 URL 优先路径同源）；上传失败 / 未取得
URL 时卡片提示"重新发送"，绝不静默丢弃；用户发送参考视频后可就绪设置
start_seconds（起始时间）与 require_audio（是否必须包含音轨）。
"""
from __future__ import annotations

import asyncio
import html
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from config import BASE_URL, SUPPORTED_MODELS
from utils import get_logger
from protocols.pipeline import resolve_request_plan

logger = get_logger(__name__)

WIZARD_CALLBACK_PREFIX = "mw:"
REPLY_MARKER = "💡 引用回复:"          # 与 app_turns 同值（避免循环导入此处复制）
_SESSION_TTL_SECONDS = 2 * 3600        # 卡片会话有效期：2 小时

# ---------------------------------------------------------------------------
# 参数声明（按模型有效参数推导卡片按钮；严格对齐官方文档）
# ---------------------------------------------------------------------------
# Agnes Image 2.5 Flash（与 2.1 同参）：size 档位 + ratio 官方集合
_IMAGE_SIZE_TIERS = ("1K", "2K", "3K", "4K")
_IMAGE_RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")
# Agnes Video 2.5：size 档位 / aspect_ratio / seconds（字符串 "4"–"12"）
_VIDEO_SIZES = ("720P", "1080P", "1K", "2K")
_VIDEO_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
_VIDEO_SECONDS = tuple(str(v) for v in range(4, 13))


@dataclass(frozen=True)
class MediaParamSpec:
    """某模型在卡片上应出现的参数集合（数据驱动渲染的唯一依据）。"""
    api_type: str                          # "images" | "video"
    size_options: tuple[str, ...] = ()
    size_label: str = "尺寸"
    size_default_hint: str = ""
    ratio_options: tuple[str, ...] = ()
    ratio_label: str = "画幅比例"
    ratio_default_hint: str = ""
    seconds_options: tuple[str, ...] = ()
    supports_mode: bool = False
    supports_frames: bool = False
    supports_ref_images: bool = False
    supports_ref_audios: bool = False
    supports_ref_videos: bool = False
    supports_seed: bool = False
    max_ref_images: int = 0
    max_ref_audios: int = 0
    max_ref_videos: int = 0
    # 模型固定、不提供按钮但应在卡片上说明的参数（如 n=1）
    fixed_notes: tuple[str, ...] = ()


def resolve_media_param_spec(model_info: Any) -> Optional[MediaParamSpec]:
    """按模型有效参数解析卡片应展示的参数集合（非媒体模型返回 None）。

    分支判断复用统一管道的 resolve_request_plan：chat 分支 -> None（不出
    卡片）；images/video 分支按端点形状给出按钮集——请求层不消费的参数
    不出现在卡片上，保证"卡片上选的每个参数都会真正进请求体"。
    """
    try:
        plan = resolve_request_plan(model_info)
    except Exception:
        return None
    if plan.api_type == "video":
        return MediaParamSpec(
            api_type="video",
            size_options=_VIDEO_SIZES,
            size_label="分辨率",
            size_default_hint="默认 720P",
            ratio_options=_VIDEO_RATIOS,
            ratio_label="画幅",
            ratio_default_hint="默认 16:9",
            seconds_options=_VIDEO_SECONDS,
            supports_mode=True,
            supports_frames=True,
            supports_ref_images=True,
            supports_ref_audios=True,
            supports_ref_videos=True,
            supports_seed=True,
            max_ref_images=8,
            max_ref_audios=3,
            max_ref_videos=1,
            # 文档：n 仅支持 1，传其他值 400 —— 只说明，不提供按钮
            fixed_notes=("数量 n = 1（模型固定）",),
        )
    if plan.api_type == "images":
        if plan.image_style == "inline_images":
            # Agnes 式 inline：size 档位 + ratio 顶层参数均被请求层消费
            return MediaParamSpec(
                api_type="images",
                size_options=_IMAGE_SIZE_TIERS,
                size_label="尺寸档位",
                size_default_hint="默认 1K",
                ratio_options=_IMAGE_RATIOS,
                ratio_label="宽高比",
                ratio_default_hint="默认 1:1",
                supports_ref_images=True,
                max_ref_images=8,
                fixed_notes=("输出由 bot 直接送达图片",),
            )
        # multipart / chat modalities 形状：请求层不消费 size/ratio/n
        return MediaParamSpec(
            api_type="images",
            supports_ref_images=True,
            max_ref_images=8,
        )
    return None


# ---------------------------------------------------------------------------
# 会话状态（每 chat 一张活跃卡片）
# ---------------------------------------------------------------------------
@dataclass
class WizardSession:
    """一张参数卡片会话的全部状态（未选择的参数保持 None = 走模型默认）。"""
    chat_id: int
    model_id: str
    api_type: str
    spec: MediaParamSpec
    prompt: str
    message_id: int = 0                    # 卡片消息（就地编辑目标）
    page: str = "main"                     # main/size/ratio/seconds/mode/frames/refs/seed/videoref:N
    # --- 已选参数（None = 未选择，提交时不发送）---
    size: Optional[str] = None
    ratio: Optional[str] = None
    seconds: Optional[str] = None          # 仅 video
    mode: Optional[str] = None             # 仅 video：None=自动
    seed: Optional[int] = None             # 仅 video
    # --- 收集的参考媒体（R2 公开 URL）---
    first_frame: Optional[str] = None      # 仅 video（keyframe）
    last_frame: Optional[str] = None
    ref_images: list[str] = field(default_factory=list)
    ref_audios: list[str] = field(default_factory=list)
    ref_videos: list[dict] = field(default_factory=list)  # {url,start_seconds?,require_audio?}
    # --- 触发消息自带的附件（file_id，提交时才解析上传）---
    pending_photos: list[dict] = field(default_factory=list)   # [{file_id,mime}]
    pending_audios: list[dict] = field(default_factory=list)
    pending_videos: list[dict] = field(default_factory=list)
    # --- 交互态 ---
    collect_slot: Optional[str] = None     # 等待用户发送的媒体槽位
    awaiting_input: Optional[str] = None   # "seed" | "start_seconds:<idx>"
    collect_error: Optional[str] = None    # 上一次收集失败的提示
    updated_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.updated_at = time.monotonic()


_sessions: dict[int, WizardSession] = {}


def _sweep_expired() -> None:
    now = time.monotonic()
    stale = [cid for cid, s in _sessions.items() if now - s.updated_at > _SESSION_TTL_SECONDS]
    for cid in stale:
        _sessions.pop(cid, None)


def get_session(chat_id: int) -> Optional[WizardSession]:
    _sweep_expired()
    return _sessions.get(chat_id)


# ---------------------------------------------------------------------------
# 输入解析：提示词清洗 / 附件提取
# ---------------------------------------------------------------------------
def clean_prompt_text(raw: str) -> str:
    """把 Telegram 消息 content 清洗成适合做生成 prompt 的文本。

    去掉引用回复前缀（保留正文）、📎 媒体占位行；仅剩通用指令语
    （"请描述这张图片的内容"等）时视为无正文返回空串。
    """
    text = str(raw or "").strip()
    if REPLY_MARKER in text:
        text = text.split(REPLY_MARKER)[-1].strip()
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("📎 用户")]
    text = "\n".join(lines).strip()
    if text in {"请描述这张图片的内容", "请分析这个视频", "请分析这段音频",
                "请分析这段语音", "请分析这段音频", "请分析这个文件"}:
        return ""
    return text


_MEDIA_KIND_ALIASES = {"photo": "photo", "image": "photo", "voice": "voice",
                       "audio": "audio", "video": "video"}
_TYPE_KIND_MAP = {"photo": "photo", "photo_group": "photo",
                  "audio": "audio", "voice": "voice",
                  "video": "video", "video_group": "video"}


def extract_media_attachments(user_message: Optional[dict]) -> list[dict]:
    """从 user_message 信封提取 [{kind, file_id, mime}]（与统一管道同口径）。"""
    out: list[dict] = []
    if not isinstance(user_message, dict):
        return out
    atts = user_message.get("attachments")
    if isinstance(atts, list):
        for att in atts:
            if not isinstance(att, dict):
                continue
            kind = _MEDIA_KIND_ALIASES.get(str(att.get("kind") or "").strip().lower())
            fid = str(att.get("file_id") or "").strip()
            if kind and fid:
                out.append({"kind": kind, "file_id": fid,
                            "mime": str(att.get("mime_type") or "")})
        if out:
            return out
    kind = _TYPE_KIND_MAP.get(str(user_message.get("type") or "").strip().lower())
    if not kind:
        return out
    mime = str(user_message.get("mime_type") or "")
    fids = user_message.get("file_ids")
    if isinstance(fids, list) and fids:
        for fid in fids:
            fid = str(fid or "").strip()
            if fid:
                out.append({"kind": kind, "file_id": fid, "mime": mime})
    else:
        fid = str(user_message.get("file_id") or "").strip()
        if fid:
            out.append({"kind": kind, "file_id": fid, "mime": mime})
    return out


# ---------------------------------------------------------------------------
# 媒体预签名 URL 解析（R2；与图片输入的统一预签名路径同源）
# ---------------------------------------------------------------------------
async def resolve_media_presigned_url(kind: str, file_id: str, mime_type: str = "") -> str:
    """把 Telegram file_id 解析为 R2 预签名 URL（媒体输入统一预签名）。

    失败 / R2 未配置返回空串——调用方据此提示用户重新上传，绝不把
    Telegram 直链（泄露 bot token）或 file:// 地址交给第三方 API。
    """
    fid = str(file_id or "").strip()
    if not fid:
        return ""
    try:
        if kind == "photo":
            from ai.attachment_content import _resolve_r2_presigned_url_for_vision
            return await _resolve_r2_presigned_url_for_vision(fid)
        if kind == "video":
            from ai.attachment_content import _resolve_r2_presigned_url_for_video
            return await _resolve_r2_presigned_url_for_video(fid, mime_type or "video/mp4")
        if kind in ("audio", "voice"):
            # 音频没有现成的预签名 URL 出口：取字节后按真实 MIME 上传 R2，
            # 再统一签发预签名 URL（与图片/视频路径同口径）。
            from ai.attachment_content import _get_cached_audio_data, _get_r2_key
            from s3_utils import generate_presigned_url, is_r2_configured, upload_bytes_to_r2
            if not is_r2_configured():
                return ""
            data = await _get_cached_audio_data(None, fid)
            if not data:
                return ""
            mime = "audio/ogg" if kind == "voice" else (mime_type or "audio/mpeg")
            r2_key = _get_r2_key(fid)
            result = await upload_bytes_to_r2(data, r2_key, mime)
            if result is None:
                return ""
            return await generate_presigned_url(r2_key)
    except Exception:
        logger.warning("媒体预签名 URL 解析失败 kind=%s fid=%s", kind, fid[:12], exc_info=True)
    return ""


# ---------------------------------------------------------------------------
# Telegram API（卡片消息的就地编辑 / 发送 / 回调应答）
# ---------------------------------------------------------------------------
async def _tg_post(method: str, payload: dict, timeout_total: int = 10) -> Optional[dict]:
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_total, connect=4)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{BASE_URL}/{method}", json=payload) as resp:
                body = await resp.text()
                if resp.status == 200:
                    try:
                        return json.loads(body).get("result")
                    except Exception:
                        return {}
                # "message is not modified"：内容未变化的重复编辑，按成功处理
                if "message is not modified" in body:
                    return {}
                logger.info("%s 未生效: status=%s body=%s", method, resp.status, body[:160])
    except Exception as e:
        logger.warning("%s 异常: %s", method, e)
    return None


async def send_card_message(chat_id: int, text: str, keyboard: Optional[dict]) -> int:
    """发送卡片消息（sendMessage），返回 message_id（失败返回 0）。"""
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard is not None:
        payload["reply_markup"] = json.dumps(keyboard)
    result = await _tg_post("sendMessage", payload)
    mid = (result or {}).get("message_id")
    return int(mid) if isinstance(mid, int) and mid > 0 else 0


async def edit_card_message(chat_id: int, message_id: int, text: str,
                            keyboard: Optional[dict]) -> bool:
    """就地编辑卡片（editMessageText）；keyboard=None 传空键盘清除按钮。"""
    payload: dict[str, Any] = {
        "chat_id": chat_id, "message_id": message_id,
        "text": text, "parse_mode": "HTML",
        "reply_markup": json.dumps(keyboard or {"inline_keyboard": []}),
    }
    return await _tg_post("editMessageText", payload) is not None


async def answer_callback(callback_id: str, text: str = "", alert: bool = False) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id, "text": text[:200]}
    if alert:
        payload["show_alert"] = True
    await _tg_post("answerCallbackQuery", payload, timeout_total=5)


# ---------------------------------------------------------------------------
# 渲染（HTML 文本 + inline keyboard；所有页面就地编辑同一条消息）
# ---------------------------------------------------------------------------
_MODE_LABELS = {"text": "文生视频", "keyframe": "首尾帧控制", "reference": "参考生成"}


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _kb(rows: list) -> dict:
    return {"inline_keyboard": [[_btn(t, d) for (t, d) in row] for row in rows]}


def _quote(text: str, limit: int = 220) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) > limit:
        clean = clean[:limit] + "…"
    return f"<blockquote>{html.escape(clean)}</blockquote>"


def _model_title(sess: WizardSession) -> str:
    model = SUPPORTED_MODELS.get(sess.model_id)
    name = model.name if model else sess.model_id
    icon = "🎬" if sess.api_type == "video" else "🖼"
    return f"{icon} <b>参数配置</b> · {html.escape(name)}"


def _pending_line(sess: WizardSession) -> str:
    parts = []
    if sess.pending_photos:
        parts.append(f"图片×{len(sess.pending_photos)}")
    if sess.pending_audios:
        parts.append(f"音频×{len(sess.pending_audios)}")
    if sess.pending_videos:
        parts.append(f"视频×{len(sess.pending_videos)}")
    if not parts:
        return ""
    return f"📎 随消息附带：{'、'.join(parts)}（提交时自动加入参考素材）"


def _effective_video_mode(sess: WizardSession) -> str:
    """卡片展示/提交校验用的生效模式（与请求层构建器推断规则一致）。"""
    if sess.mode in _MODE_LABELS:
        return sess.mode
    if sess.first_frame or sess.last_frame:
        return "keyframe"
    if sess.ref_images or sess.ref_audios or sess.ref_videos:
        return "reference"
    return "text"


def _summary_lines(sess: WizardSession) -> list[str]:
    """主页参数摘要（未选择 = 模型默认）。"""
    lines: list[str] = []
    if sess.api_type == "video":
        eff_mode = _effective_video_mode(sess)
        mode_disp = _MODE_LABELS.get(eff_mode, "自动 → 文生视频")
        if sess.mode is None:
            mode_disp = f"自动 → {mode_disp}"
        frames = []
        if sess.first_frame:
            frames.append("首帧✓")
        if sess.last_frame:
            frames.append("尾帧✓")
        refs = []
        if sess.ref_images:
            refs.append(f"图×{len(sess.ref_images)}")
        if sess.ref_audios:
            refs.append(f"音×{len(sess.ref_audios)}")
        if sess.ref_videos:
            refs.append(f"视频×{len(sess.ref_videos)}")
        pending = len(sess.pending_photos) + len(sess.pending_audios) + len(sess.pending_videos)
        lines.append(f"⏱ 时长：<b>{sess.seconds + ' 秒' if sess.seconds else '默认（5 秒）'}</b>")
        lines.append(f"🖥 分辨率：<b>{sess.size or '默认（720P）'}</b>")
        lines.append(f"📐 画幅：<b>{sess.ratio or '默认（16:9）'}</b>")
        lines.append(f"🎞 模式：<b>{mode_disp}</b>")
        lines.append("🖼 首尾帧：<b>" + ("、".join(frames) if frames else "未设置") + "</b>")
        lines.append("🎧 参考素材：<b>" +
                     ("、".join(refs) if refs else ("随消息附带" if pending else "无")) +
                     "</b>")
        lines.append(f"🎲 seed：<b>{sess.seed if sess.seed is not None else '随机'}</b>")
    else:
        lines.append(f"🖼 尺寸档位：<b>{sess.size or '默认（1K）'}</b>")
        lines.append(f"📐 宽高比：<b>{sess.ratio or '默认（1:1）'}</b>")
        refs = len(sess.ref_images) + len(sess.pending_photos)
        lines.append(f"🎨 参考图片：<b>{f'×{refs}（图生图/多图合成）' if refs else '无（文生图）'}</b>")
    for note in sess.spec.fixed_notes:
        lines.append(f"• {html.escape(note)}")
    return lines


def _collect_hint(sess: WizardSession) -> str:
    if sess.collect_slot == "first_frame":
        return "⬇️ 请在聊天中<b>直接发送首帧图片</b>（重新发送会替换当前首帧）"
    if sess.collect_slot == "last_frame":
        return "⬇️ 请在聊天中<b>直接发送尾帧图片</b>（重新发送会替换当前尾帧）"
    if sess.collect_slot == "ref_image":
        return "⬇️ 请在聊天中<b>直接发送参考图片</b>（可连发多张 / 整个相册）"
    if sess.collect_slot == "ref_audio":
        return "⬇️ 请在聊天中<b>直接发送参考音频</b>（语音或音频文件，2–12 秒最佳）"
    if sess.collect_slot == "ref_video":
        return "⬇️ 请在聊天中<b>直接发送参考视频</b>（2–12 秒，24–60FPS，≤50MB）"
    return ""


def _await_hint(sess: WizardSession) -> str:
    if sess.awaiting_input == "seed":
        return "⬇️ 请在聊天中<b>直接发送一个整数</b>作为 seed（相同 seed 结果更可复现）"
    if sess.awaiting_input and sess.awaiting_input.startswith("start_seconds:"):
        return "⬇️ 请在聊天中<b>直接发送起始秒数</b>（如 5；发送 0 表示从头开始）"
    return ""


def _picker_page(sess: WizardSession, *, title: str, current: Optional[str], options: tuple,
                 param: str, default_hint: str = "", per_row: int = 4,
                 note: str = "") -> tuple[str, dict]:
    """通用单选页：选项网格 + 默认（清除选择）+ 返回。"""
    lines = [f"⚙️ <b>{html.escape(title)}</b>", ""]
    lines.append(f"当前：<b>{html.escape(current) if current else (default_hint or '默认')}</b>")
    if note:
        lines.append(note)
    rows: list = []
    opts = list(options)
    for i in range(0, len(opts), per_row):
        rows.append([(f"✓ {o}" if o == current else o, f"mw:set:{param}:{o}")
                     for o in opts[i:i + per_row]])
    rows.append([("默认（清除选择）", f"mw:set:{param}:def")])
    rows.append([("⬅️ 返回", "mw:page:main")])
    return "\n".join(lines), _kb(rows)


def _page_mode(sess: WizardSession) -> tuple[str, dict]:
    cur = _MODE_LABELS.get(sess.mode, "自动（按素材推断）")
    lines = [
        "🎞 <b>生成模式</b>",
        "",
        f"当前：<b>{cur}</b>",
        "",
        "• 文生视频：纯提示词，不携带任何素材字段",
        "• 首尾帧控制：输入图尽量成为成片的真实首/尾帧（至少上传其一）",
        "• 参考生成：图/音频/视频作为内容、风格、动作或节奏参考",
    ]
    if sess.collect_error:
        lines.append(f"⚠️ {html.escape(sess.collect_error)}")
    rows = [
        [("✓ 自动（默认）" if sess.mode is None else "自动（默认）", "mw:set:mode:auto")],
        [("✓ 文生视频" if sess.mode == "text" else "文生视频", "mw:set:mode:text")],
        [("✓ 首尾帧控制" if sess.mode == "keyframe" else "首尾帧控制", "mw:set:mode:keyframe")],
        [("✓ 参考生成" if sess.mode == "reference" else "参考生成", "mw:set:mode:reference")],
        [("⬅️ 返回", "mw:page:main")],
    ]
    return "\n".join(lines), _kb(rows)


def _page_frames(sess: WizardSession) -> tuple[str, dict]:
    ff = "✅ 已设置" if sess.first_frame else "⬜ 未设置"
    lf = "✅ 已设置" if sess.last_frame else "⬜ 未设置"
    lines = [
        "🖼 <b>首尾帧控制（keyframe）</b>",
        "",
        f"首帧：{ff}",
        f"尾帧：{lf}",
        "",
        "文档说明：输入图会尽量保持为成片的真实首帧/尾帧，适合控制起止构图；两者至少提供其一。",
    ]
    if sess.collect_error:
        lines.append(f"⚠️ {html.escape(sess.collect_error)}")
    hint = _collect_hint(sess)
    if hint:
        lines += ["", hint]
    rows = [
        [("📤 上传首帧", "mw:collect:first_frame"), ("📤 上传尾帧", "mw:collect:last_frame")],
        [("🗑 清除首帧", "mw:clear:first_frame"), ("🗑 清除尾帧", "mw:clear:last_frame")],
        [("⬅️ 返回", "mw:page:main")],
    ]
    return "\n".join(lines), _kb(rows)


def _page_refs(sess: WizardSession) -> tuple[str, dict]:
    spec = sess.spec
    lines = ["🎧 <b>参考素材</b>", ""]
    if spec.api_type == "video":
        lines.append(f"🖼 参考图片：{len(sess.ref_images)}/{spec.max_ref_images}"
                     + ("（已添加）" if sess.ref_images else "（无）"))
        lines.append(f"🎧 参考音频：{len(sess.ref_audios)}/{spec.max_ref_audios}"
                     + ("（已添加）" if sess.ref_audios else "（无）"))
        lines.append(f"🎬 参考视频：{len(sess.ref_videos)}/{spec.max_ref_videos}"
                     + ("（已添加，点下方按钮设置起始时间/音轨）" if sess.ref_videos else "（无）"))
        lines += [
            "",
            "参考生成（reference）需要至少一类素材；提示词建议说明素材用途，"
            "如“以 &lt;Picture 1&gt; 为角色参考”（提交时自动补占位符）。",
            "文档限制：图片≤8张（宽高 256–5760px）；音频 2–12 秒；视频 2–12 秒 / 24–60FPS / ≤50MB；素材总数≤12。",
        ]
    else:
        lines.append(f"🎨 参考图片：{len(sess.ref_images)}/{spec.max_ref_images}"
                     + ("（已添加）" if sess.ref_images else "（无）"))
        lines += ["", "带参考图 = 图生图 / 多图合成；不带 = 文生图。"]
    pend = _pending_line(sess)
    if pend:
        lines += ["", pend]
    if sess.collect_error:
        lines.append(f"⚠️ {html.escape(sess.collect_error)}")
    hint = _collect_hint(sess)
    if hint:
        lines += ["", hint]
    rows: list = []
    if spec.api_type == "video":
        rows.append([("🖼 添加参考图", "mw:collect:ref_image"), ("🎧 添加音频", "mw:collect:ref_audio")])
        rows.append([("🎬 添加参考视频", "mw:collect:ref_video")])
        for i in range(1, len(sess.ref_videos) + 1):
            obj = sess.ref_videos[i - 1] or {}
            start = obj.get("start_seconds")
            ra = obj.get("require_audio")
            label = (f"🎬 视频{i}：{f'{start}秒起' if start else '从头'}"
                     f" · {'必须含音轨' if ra else '音轨不要求'}（点击设置）")
            rows.append([(label, f"mw:vrset:{i}")])
        rows.append([("🗑 清除全部", "mw:clear:refs")])
    else:
        rows.append([("🖼 添加参考图片", "mw:collect:ref_image")])
        rows.append([("🗑 清除全部", "mw:clear:refs")])
    rows.append([("⬅️ 返回", "mw:page:main")])
    return "\n".join(lines), _kb(rows)


def _page_seed(sess: WizardSession) -> tuple[str, dict]:
    cur = str(sess.seed) if sess.seed is not None else "随机（默认）"
    lines = ["🎲 <b>seed 随机种子</b>", "", f"当前：<b>{cur}</b>", "",
             "传入相同 seed 可提高结果可复现性；留空则随机。"]
    if sess.awaiting_input == "seed":
        lines += ["", _await_hint(sess)]
    rows = [
        [("🎲 输入 seed", "mw:seed:input")],
        [("🗑 清除 seed", "mw:seed:clear")],
        [("⬅️ 返回", "mw:page:main")],
    ]
    return "\n".join(lines), _kb(rows)


def _page_videoref(sess: WizardSession, idx: int) -> tuple[str, dict]:
    if not (1 <= idx <= len(sess.ref_videos)):
        sess.page = "refs"
        return _page_refs(sess)
    obj = sess.ref_videos[idx - 1] or {}
    start = obj.get("start_seconds")
    ra = obj.get("require_audio")
    lines = [
        f"🎬 <b>参考视频 {idx}</b> 设置",
        "",
        f"📍 起始时间：<b>{f'{start} 秒' if start else '开头（默认）'}</b>",
        f"🔊 音轨：<b>{'必须包含' if ra else '不要求（默认）'}</b>",
        "",
        "文档说明：start_seconds 表示从参考视频的指定秒数开始读取；"
        "require_audio 为 true 时片源必须带有音轨，否则请求会失败。",
    ]
    if sess.collect_error:
        lines.append(f"⚠️ {html.escape(sess.collect_error)}")
    hint = _await_hint(sess)
    if hint:
        lines += ["", hint]
    rows = [
        [("⏱ 设置起始秒数", f"mw:secin:{idx}")],
        [("🔊 音轨：" + ("改为不要求" if ra else "改为必须包含"), f"mw:ra:{idx}")],
        [("🗑 移除该视频", f"mw:vrdel:{idx}")],
        [("⬅️ 返回", "mw:page:refs")],
    ]
    return "\n".join(lines), _kb(rows)


def _page_main(sess: WizardSession) -> tuple[str, dict]:
    lines = [_model_title(sess), "", "📝 <b>提示词</b>", _quote(sess.prompt),
             "", "⚙️ <b>参数</b>（未选择 = 模型默认）"]
    lines += _summary_lines(sess)
    pend = _pending_line(sess)
    if pend:
        lines.append(pend)
    if sess.api_type == "video" and (sess.first_frame or sess.last_frame) \
            and (sess.ref_images or sess.ref_audios or sess.ref_videos
                 or sess.pending_photos or sess.pending_videos):
        lines.append("⚠️ 首尾帧与参考素材互斥：将按首尾帧（keyframe）模式生成，参考素材会被忽略。")
    if sess.collect_error:
        lines.append(f"⚠️ {html.escape(sess.collect_error)}")
    lines += ["", "点按钮修改参数，可随时返回本页；提交前可后退修改。"]
    if sess.api_type == "video":
        rows = [
            [("⏱ 时长", "mw:page:seconds"), ("🖥 分辨率", "mw:page:size"), ("📐 画幅", "mw:page:ratio")],
            [("🎞 生成模式", "mw:page:mode"), ("🖼 首尾帧", "mw:page:frames"), ("🎧 参考素材", "mw:page:refs")],
            [("🎲 seed", "mw:page:seed")],
            [("✅ 开始生成", "mw:submit"), ("❌ 取消", "mw:cancel")],
        ]
    else:
        rows = [
            [("🖼 尺寸档位", "mw:page:size"), ("📐 宽高比", "mw:page:ratio"), ("🎨 参考图片", "mw:page:refs")],
            [("✅ 开始生成", "mw:submit"), ("❌ 取消", "mw:cancel")],
        ]
    return "\n".join(lines), _kb(rows)


def render_page(sess: WizardSession) -> tuple[str, Optional[dict]]:
    """按会话当前页渲染卡片（HTML 文本 + inline keyboard）。"""
    page = sess.page
    if page == "size" and sess.spec.size_options:
        return _picker_page(sess, title=sess.spec.size_label, current=sess.size,
                            options=sess.spec.size_options, param="size",
                            default_hint=sess.spec.size_default_hint)
    if page == "ratio" and sess.spec.ratio_options:
        return _picker_page(sess, title=sess.spec.ratio_label, current=sess.ratio,
                            options=sess.spec.ratio_options, param="ratio",
                            default_hint=sess.spec.ratio_default_hint,
                            per_row=3 if sess.api_type == "video" else 4)
    if page == "seconds" and sess.spec.seconds_options:
        return _picker_page(sess, title="视频时长（秒）", current=sess.seconds,
                            options=sess.spec.seconds_options, param="seconds",
                            default_hint="默认 5 秒", per_row=4,
                            note="文档：seconds 为 4–12 的整数，输出时长即计费时长。")
    if page == "mode" and sess.spec.supports_mode:
        return _page_mode(sess)
    if page == "frames" and sess.spec.supports_frames:
        return _page_frames(sess)
    if page == "refs" and (sess.spec.supports_ref_images or sess.spec.supports_ref_audios
                           or sess.spec.supports_ref_videos):
        return _page_refs(sess)
    if page == "seed" and sess.spec.supports_seed:
        return _page_seed(sess)
    if page.startswith("videoref:"):
        try:
            idx = int(page.split(":", 1)[1])
        except ValueError:
            idx = 0
        return _page_videoref(sess, idx)
    return _page_main(sess)


# ---------------------------------------------------------------------------
# 卡片生命周期（回合拦截入口 / 就地重绘 / 取代旧卡）
# ---------------------------------------------------------------------------
async def _render_card(sess: WizardSession) -> bool:
    """把会话当前页就地渲染到卡片消息上（带失效守卫）。"""
    if _sessions.get(sess.chat_id) is not sess:
        return False      # 已被取代 / 已提交 / 已取消：不再编辑旧卡片
    sess.touch()
    text, keyboard = render_page(sess)
    return await edit_card_message(sess.chat_id, sess.message_id, text, keyboard)


async def _supersede_session(chat_id: int) -> None:
    old = _sessions.pop(chat_id, None)
    if old is not None and old.message_id:
        await edit_card_message(
            chat_id, old.message_id,
            "⚠️ 此卡片已被新的生成任务取代，请使用最新卡片。", None)


async def start_media_wizard_turn(chat_id: int, model_id: str,
                                  user_message: Optional[dict]) -> bool:
    """USER 回合命中图像/视频生成分支：发参数卡片并接管本回合。

    返回 True = 已出卡片（回合到此为止，调用方返回 MEDIA_WIZARD 哨兵）；
    False = 不适合出卡片（回退直接生成的旧流程，绝不阻断生成可用性）。
    """
    model_info = SUPPORTED_MODELS.get(model_id)
    spec = resolve_media_param_spec(model_info)
    if spec is None:
        return False
    raw_content = str((user_message or {}).get("content") or "")
    prompt = clean_prompt_text(raw_content) or raw_content.strip()
    if not prompt.strip():
        # 无有效 prompt（纯附件 + 通用指令）：交回原流程按旧行为处理
        return False
    atts = extract_media_attachments(user_message)
    pending_photos = [dict(a) for a in atts if a["kind"] == "photo"]
    pending_audios = [dict(a) for a in atts if a["kind"] in ("audio", "voice")]
    pending_videos = [dict(a) for a in atts if a["kind"] == "video"]
    if spec.api_type != "video":
        # 图像 API 没有音频/视频参考参数（文档白名单外）
        pending_audios, pending_videos = [], []
    await _supersede_session(chat_id)
    sess = WizardSession(
        chat_id=chat_id, model_id=model_id, api_type=spec.api_type, spec=spec,
        prompt=prompt,
        pending_photos=pending_photos, pending_audios=pending_audios,
        pending_videos=pending_videos,
    )
    text, keyboard = render_page(sess)
    mid = await send_card_message(chat_id, text, keyboard)
    if not mid:
        logger.warning("参数卡片发送失败，回退直接生成: chat=%s model=%s", chat_id, model_id)
        return False
    sess.message_id = mid
    _sessions[chat_id] = sess
    logger.info(
        "媒体参数卡片已发送: chat=%s model=%s prompt_len=%s pending(p/a/v)=%s/%s/%s",
        chat_id, model_id, len(prompt),
        len(pending_photos), len(pending_audios), len(pending_videos),
    )
    return True


# ---------------------------------------------------------------------------
# 回调处理（inline 按钮点击；卡片翻页/设置/收集/提交全部就地编辑）
# ---------------------------------------------------------------------------
def is_wizard_callback(data: Any) -> bool:
    return isinstance(data, str) and data.startswith(WIZARD_CALLBACK_PREFIX)


def _parse_int(text: str) -> Optional[int]:
    try:
        return int(str(text or "").strip())
    except (TypeError, ValueError):
        return None


def _parse_number(text: str) -> Optional[float]:
    try:
        return float(str(text or "").strip())
    except (TypeError, ValueError):
        return None


async def handle_wizard_callback(chat_id: int, uid: int, message_id: int,
                                 callback_id: str, data: str) -> None:
    """卡片回调统一入口（由 app_commands._handle_callback_query 分发）。"""
    sess = get_session(chat_id)
    if sess is None or sess.message_id != message_id:
        await answer_callback(callback_id, "卡片已失效，请重新发送提示词", alert=True)
        return
    try:
        await _dispatch_callback(sess, callback_id, data)
    except Exception:
        logger.exception("参数卡片回调处理异常: chat=%s data=%s", chat_id, data)
        await answer_callback(callback_id, "操作失败")


async def _dispatch_callback(sess: WizardSession, callback_id: str, data: str) -> None:
    rest = data[len(WIZARD_CALLBACK_PREFIX):]
    parts = rest.split(":", 2)
    action = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""

    # ---- 提交 / 取消 ----
    if action == "submit":
        await _cb_submit(sess, callback_id)
        return
    if action == "cancel":
        _sessions.pop(sess.chat_id, None)
        await edit_card_message(
            sess.chat_id, sess.message_id,
            "❌ <b>已取消</b>\n\n重新发送提示词即可再次配置生成参数。", None)
        await answer_callback(callback_id, "已取消")
        return

    # ---- 页面导航（后退/前进：所有子页返回主页或素材页）----
    if action == "page":
        target = arg or "main"
        sess.page = target if target in {
            "main", "size", "ratio", "seconds", "mode", "frames", "refs", "seed",
        } else "main"
        sess.awaiting_input = None          # 离开页面即取消一次性输入态
        if sess.page != "frames" and sess.page != "refs":
            sess.collect_error = None
        await _render_card(sess)
        await answer_callback(callback_id)
        return

    # ---- 参数设置（带白名单校验：伪造回调数据不会进请求体）----
    if action == "set":
        if arg in ("size", "ratio", "seconds", "mode"):
            key, val = arg, value
            if val != "def":
                allowed = {
                    "size": sess.spec.size_options,
                    "ratio": sess.spec.ratio_options,
                    "seconds": sess.spec.seconds_options,
                    "mode": ("text", "keyframe", "reference", "auto"),
                }.get(key, ())
                if val not in allowed:
                    await answer_callback(callback_id, "无效选项", alert=True)
                    return
            if key == "size":
                sess.size = None if val == "def" else val
            elif key == "ratio":
                sess.ratio = None if val == "def" else val
            elif key == "seconds":
                sess.seconds = None if val == "def" else val
            elif key == "mode":
                if val == "auto":
                    sess.mode = None
                    sess.page = "main"
                    await _render_card(sess)
                    await answer_callback(callback_id, "已设为自动（按素材推断模式）")
                    return
                sess.mode = val
                if val == "keyframe":
                    # 文档流程：选择首尾帧后立即引导上传（先首帧、后尾帧）
                    sess.page = "frames"
                    sess.collect_error = None
                    await _render_card(sess)
                    await answer_callback(callback_id, "请上传首帧图片（至少其一）")
                    return
                if val == "reference":
                    sess.page = "refs"
                    sess.collect_error = None
                    await _render_card(sess)
                    await answer_callback(callback_id, "请添加至少一类参考素材")
                    return
                sess.page = "main"
            sess.collect_error = None
            await _render_card(sess)
            await answer_callback(callback_id)
            return
        await answer_callback(callback_id, "无效操作", alert=True)
        return

    # ---- 进入素材收集态 ----
    if action == "collect":
        slot = arg
        if slot not in _SLOT_EXPECT:
            await answer_callback(callback_id, "无效操作", alert=True)
            return
        sess.collect_slot = slot
        sess.collect_error = None
        await _render_card(sess)
        await answer_callback(callback_id, "请在聊天中直接发送素材")
        return

    # ---- 清除 ----
    if action == "clear":
        if arg == "first_frame":
            sess.first_frame = None
        elif arg == "last_frame":
            sess.last_frame = None
        elif arg == "refs":
            sess.ref_images.clear()
            sess.ref_audios.clear()
            sess.ref_videos.clear()
            sess.pending_photos.clear()
            sess.pending_audios.clear()
            sess.pending_videos.clear()
        sess.collect_error = None
        await _render_card(sess)
        await answer_callback(callback_id, "已清除")
        return

    # ---- seed ----
    if action == "seed":
        if arg == "input":
            sess.awaiting_input = "seed"
        elif arg == "clear":
            sess.seed = None
            sess.awaiting_input = None
        await _render_card(sess)
        await answer_callback(callback_id)
        return

    # ---- 参考视频子页（起始时间 / 音轨）----
    if action in ("vrset", "secin", "ra", "vrdel"):
        idx = _parse_int(arg) or 0
        if action == "vrset":
            sess.page = f"videoref:{idx}"
        elif action == "secin":
            sess.page = f"videoref:{idx}"
            sess.awaiting_input = f"start_seconds:{idx}"
        elif action == "ra":
            if 1 <= idx <= len(sess.ref_videos):
                obj = sess.ref_videos[idx - 1]
                obj["require_audio"] = not bool(obj.get("require_audio"))
        elif action == "vrdel":
            if 1 <= idx <= len(sess.ref_videos):
                sess.ref_videos.pop(idx - 1)
            sess.page = "refs"
        sess.collect_error = None
        await _render_card(sess)
        await answer_callback(callback_id)
        return

    await answer_callback(callback_id, "未知操作")


# ---------------------------------------------------------------------------
# 消息消费钩子（app_turns 各消息处理器在进入正常回合前调用）
# ---------------------------------------------------------------------------
_SLOT_EXPECT = {
    "first_frame": "photo", "last_frame": "photo",
    "ref_image": "photo", "ref_audio": "audio", "ref_video": "video",
}
_SLOT_ACCEPT = {"photo": {"photo"}, "audio": {"audio", "voice"}, "video": {"video"}}
_KIND_LABEL = {"photo": "图片", "audio": "音频/语音", "video": "视频", "voice": "语音"}
_MEDIA_PAGES = ("frames", "refs")


def _store_into_slot(sess: WizardSession, slot: str, url: str) -> bool:
    """把素材 URL 存入对应槽位；False = 超出文档上限被忽略。"""
    if slot == "first_frame":
        sess.first_frame = url          # 重新发送即替换（用户要求的重传语义）
        return True
    if slot == "last_frame":
        sess.last_frame = url
        return True
    if slot == "ref_image":
        if len(sess.ref_images) >= max(1, sess.spec.max_ref_images):
            return False
        sess.ref_images.append(url)
        return True
    if slot == "ref_audio":
        if len(sess.ref_audios) >= max(1, sess.spec.max_ref_audios):
            return False
        sess.ref_audios.append(url)
        return True
    if slot == "ref_video":
        # 文档：参考视频最多 1 个 → 已有则替换
        sess.ref_videos = [{"url": url}]
        return True
    return False


async def try_consume_media_message(chat_id: int, user_message: Optional[dict]) -> bool:
    """卡片收集素材时接管图片/音频/视频消息。

    仅当存在活跃卡片且（正在收集，或用户停在素材相关页）时接管，避免
    误伤"发图+文字重新开卡"的正常重触发流程。返回 True = 消息已消费。

    上传失败（未取得公开访问 URL）时卡片给出错误提示并保持可重传——
    用户明确要求"没有获取预签名 URL 可以要求再次上传"，绝不静默丢弃。

    可观测性（2026-09-12）：消费分支一律 INFO 留痕。此前被卡片消费的
    消息无任何日志，一旦用户反馈"发两张图只处理了一张"，无法从日志
    区分"被卡片消费"还是"回合被打断丢失"（后者是当时真实存在的
    缺陷），排查成本极高。
    """
    sess = get_session(chat_id)
    if sess is None:
        return False
    atts = extract_media_attachments(user_message)
    if not atts:
        return False
    on_media_page = sess.page in _MEDIA_PAGES or sess.page.startswith("videoref:")
    slot = sess.collect_slot
    if slot is None and not on_media_page:
        return False

    if slot is None:
        # 停在素材页但未进入收集态：提示先点按钮（消费掉消息，避免触发
        # 一次莫名其妙"缺 prompt"的生成回合）
        sess.collect_error = "请先点击对应的『上传/添加』按钮，再发送素材。"
        await _render_card(sess)
        logger.info(
            "[media-wizard] chat=%s 已消费媒体消息（未进入收集态）：kind=%s",
            chat_id, atts[0].get("kind"),
        )
        return True

    expect = _SLOT_EXPECT[slot]
    matched = [a for a in atts if a["kind"] in _SLOT_ACCEPT[expect]]
    if not matched:
        sess.collect_error = (
            f"当前需要的是{_KIND_LABEL.get(expect, expect)}，"
            f"本次发送的{_KIND_LABEL.get(atts[0]['kind'], atts[0]['kind'])}已忽略。"
        )
        await _render_card(sess)
        logger.info(
            "[media-wizard] chat=%s 已消费媒体消息（类型不匹配 slot=%s）：kind=%s",
            chat_id, slot, atts[0].get("kind"),
        )
        return True

    sess.collect_slot = None
    sess.collect_error = None
    added, failed = 0, 0
    for att in matched:
        url = await resolve_media_presigned_url(
            att["kind"], att.get("file_id", ""), att.get("mime") or "")
        if url and _store_into_slot(sess, slot, url):
            added += 1
        else:
            failed += 1
    if failed and not added:
        # 全部失败：保持收集态，要求再次上传（绝不静默丢弃）
        sess.collect_slot = slot
        sess.collect_error = (
            "上传失败：未能取得素材的 R2 预签名 URL。"
            "请重新发送该素材再试一次。"
        )
    elif failed:
        sess.collect_slot = None
        sess.collect_error = f"已添加 {added} 个，{failed} 个上传失败；可重新点击按钮补齐。"
    sess.page = {"first_frame": "frames", "last_frame": "frames"}.get(slot, "refs")
    if slot == "ref_video" and sess.ref_videos:
        # 用户要求的流程：发完参考视频立即进入其设置页（起始时间/音轨）
        sess.page = f"videoref:{len(sess.ref_videos)}"
    await _render_card(sess)
    logger.info(
        "[media-wizard] chat=%s 已消费媒体消息并入槽 slot=%s：added=%d failed=%d kind=%s",
        chat_id, slot, added, failed, matched[0].get("kind") if matched else "?",
    )
    return True


async def try_consume_text_message(chat_id: int, raw_text: str) -> bool:
    """卡片会话接管文本消息：seed/起始秒数输入，或更新提示词。

    返回 True = 消息已消费（不进入正常回合）。无活跃卡片时恒为 False。
    消费分支 INFO 留痕（同 try_consume_media_message 的可观测性说明）。
    """
    sess = get_session(chat_id)
    if sess is None:
        return False
    text = clean_prompt_text(raw_text)

    if sess.awaiting_input == "seed":
        value = _parse_int(text)
        if value is None:
            sess.collect_error = "无法识别的 seed，请直接发送一个整数（如 42）；点『返回』可取消输入。"
        else:
            sess.seed = value
            sess.awaiting_input = None
            sess.collect_error = None
        sess.page = "seed"
        await _render_card(sess)
        logger.info("[media-wizard] chat=%s 已消费文本消息（seed 输入）", chat_id)
        return True

    if sess.awaiting_input and sess.awaiting_input.startswith("start_seconds:"):
        try:
            idx = int(sess.awaiting_input.split(":", 1)[1])
        except ValueError:
            idx = 0
        value = _parse_number(text)
        if value is None or value < 0:
            sess.collect_error = "无法识别的秒数，请发送非负数字（如 5 或 5.5）；点『返回』可取消输入。"
        else:
            if 1 <= idx <= len(sess.ref_videos):
                if value > 0:
                    sess.ref_videos[idx - 1]["start_seconds"] = (
                        int(value) if float(value).is_integer() else value)
                else:
                    sess.ref_videos[idx - 1].pop("start_seconds", None)
            sess.awaiting_input = None
            sess.collect_error = None
        sess.page = f"videoref:{idx}" if idx else "refs"
        await _render_card(sess)
        logger.info("[media-wizard] chat=%s 已消费文本消息（起始秒数输入）", chat_id)
        return True

    if not text:
        return False
    # 普通文本：更新提示词，卡片保持（用户可继续配置或直接提交）
    sess.prompt = text
    sess.page = "main"
    sess.collect_error = None
    await _render_card(sess)
    return True


# ---------------------------------------------------------------------------
# 提交：构造生成请求并作为 turn 任务执行
# ---------------------------------------------------------------------------
def build_submission(sess: WizardSession) -> tuple[Optional[dict], str, str]:
    """校验并构造提交请求。返回 (request, 错误跳转页, 错误提示)。

    未选择的参数不进 overrides（请求体不携带 → 走网关默认）；video 的
    keyframe/reference 显式模式按文档校验素材前置，不满足时跳转到对应
    补素材页面并给出提示。
    """
    spec = sess.spec
    if not sess.prompt.strip():
        return None, "main", "提示词为空，请先发送生成内容描述"
    overrides: dict[str, Any] = {}
    summary_bits: list[str] = []

    if spec.api_type == "video":
        eff = _effective_video_mode(sess)
        if sess.mode == "keyframe" and not (sess.first_frame or sess.last_frame):
            return None, "frames", "首尾帧模式需要至少上传一张首帧或尾帧图片"
        if sess.mode == "reference" and not (
            sess.ref_images or sess.ref_audios or sess.ref_videos
            or sess.pending_photos or sess.pending_audios or sess.pending_videos
        ):
            return None, "refs", "参考生成模式需要至少一类参考素材"
        if sess.seconds:
            overrides["seconds"] = sess.seconds
            summary_bits.append(f"{sess.seconds}秒")
        if sess.size:
            overrides["size"] = sess.size
            summary_bits.append(sess.size)
        if sess.ratio:
            overrides["aspect_ratio"] = sess.ratio
            summary_bits.append(sess.ratio)
        if sess.mode:
            overrides["mode"] = sess.mode
        if sess.seed is not None:
            overrides["seed"] = sess.seed
            summary_bits.append(f"seed={sess.seed}")
        if sess.first_frame:
            overrides["first_frame"] = sess.first_frame
        if sess.last_frame:
            overrides["last_frame"] = sess.last_frame
        if sess.ref_audios:
            overrides["reference_audios"] = list(sess.ref_audios)
        if sess.ref_videos:
            overrides["video_specs"] = [dict(v) for v in sess.ref_videos]
        if sess.ref_images:
            overrides["reference_images"] = list(sess.ref_images)
        summary_bits.append({"text": "文生视频", "keyframe": "首尾帧", "reference": "参考生成"}[eff])
    else:
        if sess.size:
            overrides["image_size"] = sess.size
            summary_bits.append(sess.size)
        if sess.ratio:
            overrides["aspect_ratio"] = sess.ratio
            summary_bits.append(sess.ratio)
        if sess.ref_images:
            overrides["reference_images"] = list(sess.ref_images)

    param_line = ' · '.join(html.escape(b) for b in summary_bits) or '模型默认参数'
    summary = (
        "⏳ <b>正在生成…</b>\n\n"
        f"📝 {_quote(sess.prompt, 160)}\n\n"
        f"⚙️ {param_line}\n"
        "（完成后本卡片会自动更新为结果，可随时发新消息打断）"
    )
    request = {
        "chat_id": sess.chat_id,
        "model_id": sess.model_id,
        "api_type": spec.api_type,
        "prompt": sess.prompt,
        "overrides": overrides,
        "pending_photos": [dict(a) for a in sess.pending_photos],
        "pending_audios": [dict(a) for a in sess.pending_audios],
        "pending_videos": [dict(a) for a in sess.pending_videos],
        "summary": summary,
        "prompt_preview": _quote(sess.prompt, 160),
        "param_line": param_line,
    }
    return request, "", ""


async def _cb_submit(sess: WizardSession, callback_id: str) -> None:
    request, err_page, err_msg = build_submission(sess)
    if request is None:
        sess.page = err_page or "main"
        await _render_card(sess)
        await answer_callback(callback_id, err_msg or "无法提交", alert=True)
        return
    _sessions.pop(sess.chat_id, None)
    # 卡片提交后不再是终态文案——真正的完成/失败由 run_media_generation
    # 在生成结束时就地编辑同一条卡片消息（见 _finalize_card），避免卡片
    # 停留在"进行中"状态、观感像是卡住。
    request["message_id"] = sess.message_id
    await edit_card_message(sess.chat_id, sess.message_id, request["summary"], None)
    await answer_callback(callback_id, "已提交，开始生成…")
    try:
        from app_turns import spawn_turn_task
        await spawn_turn_task(sess.chat_id, run_media_generation(sess.chat_id, request))
    except Exception:
        logger.exception("生成任务派发失败: chat=%s", sess.chat_id)
        await edit_card_message(
            sess.chat_id, sess.message_id,
            "❌ <b>生成任务派发失败</b>，请重试。", None)


async def _finalize_card(
    chat_id: int, message_id: int, request: dict, *, ok: bool, note: str = "",
) -> None:
    """把提交卡片就地编辑为终态（成功/失败），不再让它停在"进行中"。

    结果媒体（图片/视频）本身仍由生成循环作为独立消息直发——Telegram
    无法把媒体塞进一条已存在的纯文本消息——但卡片自身必须显示与之对应
    的终态，否则用户看到的是"卡片卡住 + 平白多出一条消息"的割裂体验。
    """
    if not message_id:
        return
    prompt_preview = request.get("prompt_preview") or ""
    param_line = request.get("param_line") or "模型默认参数"
    if ok:
        text = (
            "✅ <b>生成完成</b>\n\n"
            f"📝 {prompt_preview}\n\n"
            f"⚙️ {param_line}\n"
            "结果已在上方/下方消息中发送。"
        )
    else:
        text = (
            "❌ <b>生成失败</b>\n\n"
            f"📝 {prompt_preview}\n\n"
            f"⚙️ {param_line}\n"
            f"原因：{html.escape(note)[:300] or '未知错误'}"
        )
    try:
        await edit_card_message(chat_id, message_id, text, None)
    except Exception:
        logger.debug("卡片终态编辑失败（可忽略）: chat=%s", chat_id, exc_info=True)


async def _notify_generation_failure(
    chat_id: int, notice: str, *, message_id: int = 0, request: Optional[dict] = None,
) -> None:
    """生成失败通知（与 IMAGE/VIDEO_ERROR 的渲染语义一致）。

    渲染复用 ``_render_media_failure_quote``（ai.error_formatting）——
    修复（2026-09 生产事故）：notice 来自 ``IMAGE_ERROR:``/``VIDEO_ERROR:``
    信号，本身已是 Telegram HTML（如 ``⚠️ <b>… 请求失败</b>…``），此前
    ``html.escape(notice)`` 把标签再次转义，用户看到的是 ``&lt;b&gt;``
    字面量而非加粗标题。改走与 ai_handlers IMAGE_ERROR/VIDEO_ERROR 完全
    相同的渲染出口：unescape → 剥标签 → 严格转义后放入 <pre> 结果块，
    纯文本 notice 同样安全。

    同时把提交卡片（若提供 message_id）编辑为"❌ 生成失败"终态，避免卡片
    停在"进行中"而失败提示只出现在另一条不相关的新消息里。
    """
    if message_id and request is not None:
        await _finalize_card(chat_id, message_id, request, ok=False, note=notice)
    try:
        from utils import send_rich_html_message
        from ai.error_formatting import _render_media_failure_quote
        # pre_rendered=True：引用块是严格转义的最终 HTML，发送层不重过
        # Markdown 转换器（与 ai_handlers IMAGE/VIDEO_ERROR 出口同援）。
        await send_rich_html_message(chat_id, _render_media_failure_quote(notice), pre_rendered=True)
    except Exception:
        logger.warning("生成失败通知发送失败: chat=%s", chat_id, exc_info=True)
    try:
        import turn_recovery
        await turn_recovery.mark_failed_unanswered_user(chat_id)
    except Exception:
        logger.debug("mark_failed_unanswered_user 失败（可忽略）", exc_info=True)


async def run_media_generation(chat_id: int, request: dict) -> None:
    """执行卡片提交的生成（turn 任务；结果媒体由媒体循环直接发送）。

    触发消息自带的附件在这里才解析为 R2 预签名 URL（卡片发送零等待）；
    解析失败的附件计数并提示，绝不静默丢弃。
    """
    from core.messages import Message, TextBlock

    model_id = request["model_id"]
    api_type = request["api_type"]
    prompt = request["prompt"]
    overrides = dict(request.get("overrides") or {})
    card_message_id = int(request.get("message_id") or 0)

    dropped = 0
    resolved_images: list[str] = []
    for att in request.get("pending_photos") or []:
        url = await resolve_media_presigned_url(
            "photo", str(att.get("file_id") or ""), str(att.get("mime") or ""))
        if url:
            resolved_images.append(url)
        else:
            dropped += 1
    resolved_audios: list[str] = []
    for att in request.get("pending_audios") or []:
        url = await resolve_media_presigned_url(
            str(att.get("kind") or "audio"), str(att.get("file_id") or ""),
            str(att.get("mime") or ""))
        if url:
            resolved_audios.append(url)
        else:
            dropped += 1
    resolved_specs: list[dict] = []
    for att in request.get("pending_videos") or []:
        url = await resolve_media_presigned_url(
            "video", str(att.get("file_id") or ""), str(att.get("mime") or ""))
        if url:
            resolved_specs.append({"url": url})
        else:
            dropped += 1
    if resolved_images:
        overrides["reference_images"] = resolved_images + list(overrides.get("reference_images") or [])
    if resolved_audios:
        overrides["reference_audios"] = resolved_audios + list(overrides.get("reference_audios") or [])
    if resolved_specs:
        overrides["video_specs"] = resolved_specs + list(overrides.get("video_specs") or [])
    if dropped:
        await send_card_message(
            chat_id,
            f"⚠️ 有 {dropped} 个随消息附带的素材上传失败（未取得预签名 URL），已跳过。",
            None,
        )

    messages = [Message.user([TextBlock(prompt)])]
    try:
        if api_type == "video":
            from ai.agentic_loops import _agentic_loop_native_video
            raw, usage, new_msgs = await _agentic_loop_native_video(
                model_id, messages, None, chat_id, media_overrides=overrides)
        else:
            from ai.agentic_loops import _agentic_loop_native_image
            raw, usage, new_msgs = await _agentic_loop_native_image(
                None, model_id, messages, None, chat_id, media_overrides=overrides)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("卡片提交生成异常: chat=%s model=%s", chat_id, model_id)
        await _notify_generation_failure(
            chat_id, f"生成任务异常: {str(e)[:200]}",
            message_id=card_message_id, request=request)
        return

    if isinstance(raw, str) and raw.startswith(("VIDEO_ERROR:", "IMAGE_ERROR:")):
        await _notify_generation_failure(
            chat_id, raw.split(":", 1)[1].strip(),
            message_id=card_message_id, request=request)
        return

    # 成功：媒体已由循环直发；卡片就地编辑为"生成完成"终态，不再停在
    # "进行中"；assistant 结果沉淀历史（user prompt 已在卡片拦截轮写入
    # 历史，这里传 None 不重复写）。
    await _finalize_card(chat_id, card_message_id, request, ok=True)
    try:
        from app_turns import update_conversation_and_ledger
        await update_conversation_and_ledger(chat_id, None, new_msgs, usage)
    except Exception:
        logger.debug("卡片生成结果沉淀历史失败（可忽略）", exc_info=True)


__all__ = [
    "MediaParamSpec",
    "WizardSession",
    "WIZARD_CALLBACK_PREFIX",
    "resolve_media_param_spec",
    "resolve_media_presigned_url",
    "clean_prompt_text",
    "extract_media_attachments",
    "get_session",
    "start_media_wizard_turn",
    "is_wizard_callback",
    "handle_wizard_callback",
    "render_page",
    "build_submission",
    "try_consume_media_message",
    "try_consume_text_message",
    "run_media_generation",
]
