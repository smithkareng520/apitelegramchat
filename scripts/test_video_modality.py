# 视频输入模态改动验证脚本
# 1) config: video 能力标志正确标注
# 2) attachment_content: mime 归一化 + _resolve_multimodal_content 视频分支
# 3) 模拟"切换模型不丢信息"：同一条历史视频消息按两个模型分别重解析
import asyncio
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))

# 提供最小环境变量避免 config 导入告警
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

from apitelegramchat.config import SUPPORTED_MODELS, make_model_config

# ---------- 1) 配置检查 ----------
ox = SUPPORTED_MODELS["stealth/ox-alpha"]
g37 = SUPPORTED_MODELS["gemini-3.7-flash"]
g35 = SUPPORTED_MODELS["gemini-3.5-flash-lite"]
deepseek = SUPPORTED_MODELS["deepseek-ai/DeepSeek-V4-Flash-0731"]
claude = SUPPORTED_MODELS["anthropic/claude-sonnet-5"]

assert ox.video is True, "stealth/ox-alpha 应支持视频输入"
assert g37.video is True, "gemini-3.7-flash 应支持视频输入"
assert g35.video is True, "gemini-3.5-flash-lite 应支持视频输入"
assert not deepseek.video, "DeepSeek 不应默认支持视频输入"
assert not claude.video, "Claude Sonnet 5 input_modalities=[text,image,file]，无 video"
# 输入模态 video 与生成模态 native_video 相互独立
assert SUPPORTED_MODELS["agnes-video-v2.0"].native_video is True
assert SUPPORTED_MODELS["agnes-video-v2.0"].video is not True
print("[OK] 1) 配置标志: ox-alpha/gemini 均为 video=True，其余默认 False")

# ---------- 2) mime 归一化 ----------
from apitelegramchat.ai.attachment_content import (
    _normalize_video_mime_type,
    _resolve_multimodal_content,
)

assert _normalize_video_mime_type("video/mp4") == "video/mp4"
assert _normalize_video_mime_type("video/webm") == "video/webm"
assert _normalize_video_mime_type("video/quicktime") == "video/mov"
assert _normalize_video_mime_type("") == "video/mp4"
assert _normalize_video_mime_type("application/octet-stream") == "video/mp4"
assert _normalize_video_mime_type(None) == "video/mp4"
print("[OK] 2) mime 归一化: quicktime→mov, 未知→mp4")

# ---------- 3) 视频分支解析 ----------
import apitelegramchat.ai.attachment_content as ac

# mock R2 URL 解析：可控返回
original_resolver = ac._resolve_r2_public_url_for_video
original_persist = ac._ensure_video_persisted
original_track = ac._track_task

persist_calls = []


async def fake_persist(fid, mime="video/mp4"):
    persist_calls.append(fid)


async def run_case(name, supports_video, url_result):
    async def fake_url(fid, mime="video/mp4"):
        return url_result

    tracked_tasks = []

    def fake_track(coro):
        # 真正调度协程，让 fake_persist 执行并记录
        tracked_tasks.append(asyncio.ensure_future(coro))
        return tracked_tasks[-1]

    ac._resolve_r2_public_url_for_video = fake_url
    ac._ensure_video_persisted = fake_persist
    ac._track_task = fake_track

    model_info = make_model_config(
        model_id="test/video-model" if supports_video else "test/text-model",
        provider="openrouter",
        name="Test",
        video=supports_video,
    )
    msg = {
        "role": "user",
        "content": "📎 用户上传了视频「demo.mp4」\n\n总结这个视频",
        "file_id": "FILE123",
        "file_name": "demo.mp4",
        "mime_type": "video/mp4",
        "type": "video",
    }
    result = await _resolve_multimodal_content(msg, model_info, "openrouter", chat_id=100)
    # 等待后台持久化任务跑完
    for t in tracked_tasks:
        await t
    return result


# 3a. 支持视频 + URL 可用 → video_url content part
persist_calls.clear()
r = asyncio.run(run_case("native", True, "https://r2.example.com/telegram/FILE123"))
assert isinstance(r, list) and len(r) == 2, f"应返回 [video_url, text]，实际: {r}"
assert r[0] == {
    "type": "video_url",
    "video_url": {"url": "https://r2.example.com/telegram/FILE123"},
}, f"video_url part 不符: {r[0]}"
assert r[1]["type"] == "text" and "总结这个视频" in r[1]["text"]
print("[OK] 3a) 支持视频模型 → [video_url, text] 结构正确")

# 3b. 支持视频 + URL 不可用 → 文本降级 + 触发后台持久化
persist_calls.clear()
r = asyncio.run(run_case("degraded", True, ""))
assert isinstance(r, str) and "📎 用户上传了视频" in r and "file_id：FILE123" in r, f"降级文本不符: {r[:200]}"
assert "R2" in r, "降级文本应说明 R2 不可用"
assert persist_calls == ["FILE123"], f"应触发后台持久化: {persist_calls}"
print("[OK] 3b) URL 不可用 → 文本降级 + 后台持久化已触发")

# 3c. 不支持视频 → 文本降级 + 后台持久化（保证未来切换模型不丢）
persist_calls.clear()
r = asyncio.run(run_case("fallback", False, "https://unused"))
assert isinstance(r, str) and "📎 用户上传了视频" in r
assert persist_calls == ["FILE123"], f"不支持视频也应后台持久化: {persist_calls}"
print("[OK] 3c) 不支持视频模型 → 文本降级 + 后台持久化已触发")

# ---------- 4) 模拟切换模型重解析 ----------
# 同一条历史消息（含元数据），先在"不支持视频"模型下降级，
# 再切到"支持视频"模型重新解析为原生 video_url —— 信息不丢失。

ac._resolve_r2_public_url_for_video = original_resolver
ac._ensure_video_persisted = original_persist
ac._track_task = original_track

# mock s3 层模拟 R2 已持久化（切换模型后的热路径）
import apitelegramchat.ai.attachment_content as ac2
from apitelegramchat.ai import attachment_content as ac_mod


async def fake_exists(key):
    return key == "telegram/FILE123"


async def fake_public_url(key):
    return f"https://cdn.example.com/{key}"


ac_mod.file_exists_in_r2 = fake_exists
ac_mod.public_url_for_existing_key = fake_public_url
ac_mod.is_r2_configured = lambda: True

history_msg = {
    "role": "user",
    "content": "看看我发的视频",
    "file_id": "FILE123",
    "file_name": "demo.mp4",
    "mime_type": "video/mp4",
    "type": "video",
}


async def switch_test():
    text_model = make_model_config("test/text-only", "openrouter", "TextOnly")
    video_model = make_model_config("test/video-ok", "openrouter", "VideoOK", video=True)

    # 第一轮：不支持视频的模型 → 文本
    as_text = await _resolve_multimodal_content(
        dict(history_msg), text_model, "openrouter", chat_id=1
    )
    # 第二轮：切换到支持视频的模型 → 原生 video_url（R2 已有对象，直接命中）
    as_video = await _resolve_multimodal_content(
        dict(history_msg), video_model, "openrouter", chat_id=1
    )
    return as_text, as_video


as_text, as_video = asyncio.run(switch_test())
assert isinstance(as_text, str) and "📎" in as_text, "文本模型应收到降级文本"
assert (
    isinstance(as_video, list)
    and as_video[0]["type"] == "video_url"
    and as_video[0]["video_url"]["url"] == "https://cdn.example.com/telegram/FILE123"
), f"切换后应恢复原生视频: {as_video}"
print("[OK] 4) 切换模型重解析: 文本降级 → 切到视频模型后恢复原生 video_url ✓")

print("\n全部测试通过 ✅")
