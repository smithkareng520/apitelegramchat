# video_group（视频相册）分支验证
import asyncio
import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

from apitelegramchat.config import make_model_config
from apitelegramchat.ai import attachment_content as ac
from apitelegramchat.ai.attachment_content import _resolve_multimodal_content

# mock：两个视频的 URL 都可解析
async def fake_url_ok(fid, mime="video/mp4"):
    return f"https://cdn.example.com/telegram/{fid}"


persist_calls = []


async def fake_persist(fid, mime="video/mp4"):
    persist_calls.append(fid)


def fake_track(coro):
    return asyncio.ensure_future(coro)


async def main():
    video_model = make_model_config("test/video-ok", "openrouter", "VideoOK", video=True)
    text_model = make_model_config("test/text-only", "openrouter", "TextOnly")

    group_msg = {
        "role": "user",
        "content": "📎 用户上传了视频组（共 2 个）\n\n对比这两段视频",
        "file_ids": ["VID_A", "VID_B"],
        "file_names": ["a.mp4", "b.mp4"],
        "mime_types": ["video/mp4", "video/webm"],
        "type": "video_group",
        "attachments": [],
    }

    # 1) 支持视频的模型 → 两个 video_url part + text
    ac._resolve_r2_public_url_for_video = fake_url_ok
    r = await _resolve_multimodal_content(dict(group_msg), video_model, "openrouter", chat_id=1)
    assert isinstance(r, list), f"应为数组: {type(r)}"
    video_parts = [p for p in r if p.get("type") == "video_url"]
    assert len(video_parts) == 2, f"应有 2 个 video_url: {len(video_parts)}"
    assert video_parts[0]["video_url"]["url"] == "https://cdn.example.com/telegram/VID_A"
    assert video_parts[1]["video_url"]["url"] == "https://cdn.example.com/telegram/VID_B"
    assert r[-1]["type"] == "text" and "对比这两段视频" in r[-1]["text"]
    print("[OK] vg-1) 视频组 + 支持视频模型 → [video_url ×2, text] ✓")

    # 2) 不支持视频的模型 → 文本降级 + 两个都后台持久化
    persist_calls.clear()
    ac._ensure_video_persisted = fake_persist
    ac._track_task = fake_track

    async def fake_url_unused(fid, mime="video/mp4"):
        raise AssertionError("不应解析 URL")

    ac._resolve_r2_public_url_for_video = fake_url_unused
    r = await _resolve_multimodal_content(dict(group_msg), text_model, "openrouter", chat_id=1)
    assert isinstance(r, str) and "视频组" in r or "视频" in r, f"降级文本: {r[:150]}"
    await asyncio.sleep(0.05)  # 等后台任务
    assert sorted(persist_calls) == ["VID_A", "VID_B"], f"两个都应持久化: {persist_calls}"
    print("[OK] vg-2) 视频组 + 不支持视频模型 → 文本降级 + 双视频后台持久化 ✓")

    # 3) 支持视频但部分 URL 失败 → 只保留成功的 part
    persist_calls.clear()

    async def fake_url_partial(fid, mime="video/mp4"):
        return f"https://cdn.example.com/telegram/{fid}" if fid == "VID_A" else ""

    ac._resolve_r2_public_url_for_video = fake_url_partial
    r = await _resolve_multimodal_content(dict(group_msg), video_model, "openrouter", chat_id=1)
    video_parts = [p for p in r if p.get("type") == "video_url"]
    assert len(video_parts) == 1 and video_parts[0]["video_url"]["url"].endswith("VID_A")
    await asyncio.sleep(0.05)
    assert persist_calls == ["VID_B"], f"失败的应触发持久化: {persist_calls}"
    print("[OK] vg-3) 部分失败 → 成功的照常发送 + 失败的后台持久化 ✓")


asyncio.run(main())
print("\nvideo_group 测试全部通过 ✅")
