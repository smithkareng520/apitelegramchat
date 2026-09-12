# -*- coding: utf-8 -*-
"""回复引用媒体补齐（"回复消息带上全部图片/音频等"）单元测试。

覆盖场景（对应 state.album_media_registry / app_turns._get_reply_media /
_get_reply_context / proactive.WAKEUP_PROMPT 的行为契约）：

  - 相册聚合时登记整组媒体（图片取最大尺寸 PhotoSize，去重）；
  - 回复相册任意分片：reply_to_message 只带一个分片，登记表补齐整组；
  - 登记表未命中（bot 重启 / LRU 淘汰）：退化为单分片引用（旧行为）；
  - 登记表有界（LRU 200 条）与 chat 归属隔离；
  - 单媒体消息（photo/document/audio/voice/video/video_note）单元素列表；
  - 无 reply_to_message 返回空列表；
  - 引用占位文本：无 caption 媒体给出具体类型描述，有 caption 用原文；
  - TIMER 唤醒提示词：对任务/记忆用能力描述而非指令式"调用 todo 的
    list"，避免无对应工具的模型在正文里幻觉 tool_calls JSON。
"""
import asyncio

import state
from app_turns import _get_reply_context, _get_reply_media
from state import (
    album_media_registry,
    get_album_media,
    record_album_media,
)


PHOTO_SHARDS = [
    {
        "message_id": 101,
        "photo": [{"file_id": "small1", "width": 90}, {"file_id": "big1", "width": 1280}],
        "caption": "cat",
    },
    {"message_id": 102, "photo": [{"file_id": "small2"}, {"file_id": "big2"}]},
    {"message_id": 103, "photo": [{"file_id": "small3"}, {"file_id": "big3"}]},
]


def _reply_msg(reply: dict, chat_id: int = 777) -> dict:
    return {"chat": {"id": chat_id}, "reply_to_message": reply}


# ----------------------------------------------------------------------
# state 相册媒体登记表
# ----------------------------------------------------------------------

def test_record_album_media_collects_all_photos():
    state.album_media_registry.clear()
    record_album_media("MG-1", 777, PHOTO_SHARDS)
    entry = get_album_media(777, "MG-1")
    assert entry is not None
    # 每个分片取最大尺寸 PhotoSize，顺序保持分片顺序，去重
    assert entry["photos"] == ["big1", "big2", "big3"]
    assert entry["captions"] == ["cat"]


def test_record_album_media_collects_video_audio_document():
    state.album_media_registry.clear()
    record_album_media("MG-2", 777, [
        {"message_id": 201, "video": {"file_id": "v1", "mime_type": "video/mp4"}},
        {"message_id": 202, "audio": {"file_id": "a1", "file_name": "song.mp3",
                                       "mime_type": "audio/mpeg"}},
        {"message_id": 203, "document": {"file_id": "d1", "file_name": "doc.pdf",
                                         "mime_type": "application/pdf"}},
    ])
    entry = get_album_media(777, "MG-2")
    assert entry["videos"][0]["file_id"] == "v1"
    assert entry["audios"][0]["file_name"] == "song.mp3"
    assert entry["documents"][0]["file_name"] == "doc.pdf"


def test_registry_isolated_by_chat():
    state.album_media_registry.clear()
    record_album_media("MG-iso", 777, PHOTO_SHARDS)
    assert get_album_media(888, "MG-iso") is None
    assert get_album_media(777, "MG-iso") is not None
    assert get_album_media(777, "MG-unknown") is None


def test_registry_lru_bounded():
    state.album_media_registry.clear()
    for i in range(250):
        record_album_media(f"MG-fill-{i}", 777, PHOTO_SHARDS)
    assert len(album_media_registry) == 200
    # 最早的登记被 LRU 淘汰
    assert get_album_media(777, "MG-fill-0") is None
    assert get_album_media(777, "MG-fill-249") is not None


# ----------------------------------------------------------------------
# _get_reply_media：相册整组补齐 + 单媒体列表化
# ----------------------------------------------------------------------

def test_reply_to_album_returns_all_photos():
    state.album_media_registry.clear()
    record_album_media("MG-live", 777, PHOTO_SHARDS)
    # 用户回复相册第 2 个分片：reply_to_message 只携带该分片
    items = _get_reply_media(_reply_msg({
        "message_id": 102,
        "media_group_id": "MG-live",
        "photo": [{"file_id": "small2"}, {"file_id": "big2"}],
    }))
    assert [it["file_id"] for it in items] == ["big1", "big2", "big3"]
    assert all(it["kind"] == "photo" for it in items)


def test_reply_to_album_registry_miss_falls_back_to_single_shard():
    state.album_media_registry.clear()
    items = _get_reply_media(_reply_msg({
        "message_id": 102,
        "media_group_id": "MG-gone",
        "photo": [{"file_id": "small2"}, {"file_id": "big2"}],
    }))
    # bot 重启 / 登记淘汰：退化为单分片（旧行为），不报错
    assert len(items) == 1
    assert items[0]["file_id"] == "big2"
    assert items[0]["kind"] == "photo"


def test_reply_media_single_media_messages():
    state.album_media_registry.clear()
    cases = [
        ({"photo": [{"file_id": "s"}, {"file_id": "big"}]}, "photo", "big"),
        ({"document": {"file_id": "d", "file_name": "a.pdf"}}, "document", "d"),
        ({"audio": {"file_id": "a", "file_name": "x.mp3"}}, "audio", "a"),
        ({"voice": {"file_id": "v"}}, "voice", "v"),
        ({"video": {"file_id": "vd"}}, "video", "vd"),
        ({"video_note": {"file_id": "vn"}}, "video", "vn"),
    ]
    for reply, want_kind, want_fid in cases:
        items = _get_reply_media(_reply_msg(reply))
        assert len(items) == 1, reply
        assert items[0]["kind"] == want_kind
        assert items[0]["file_id"] == want_fid


def test_reply_media_empty_without_reply():
    assert _get_reply_media({"chat": {"id": 1}}) == []


# ----------------------------------------------------------------------
# _get_reply_context：媒体占位描述
# ----------------------------------------------------------------------

def test_reply_context_describes_captionless_media():
    ctx = _get_reply_context(_reply_msg({"photo": [{"file_id": "x"}]}))
    assert "[图片，无文字说明]" in ctx
    ctx_voice = _get_reply_context(_reply_msg({"voice": {"file_id": "x"}}))
    assert "[语音，无文字说明]" in ctx_voice


def test_reply_context_prefers_quote_and_caption():
    ctx = _get_reply_context(_reply_msg({"text": "hello world"}))
    assert "hello world" in ctx
    ctx_cap = _get_reply_context(_reply_msg(
        {"photo": [{"file_id": "x"}], "caption": "看这张图"}))
    assert "看这张图" in ctx_cap


# ----------------------------------------------------------------------
# WAKEUP_PROMPT：去工具化表述（防幻觉 tool_calls）
# ----------------------------------------------------------------------

def test_wakeup_prompt_avoids_tool_invocation_instructions():
    import proactive

    wp = proactive.WAKEUP_PROMPT
    # 不再指令式要求调用 todo 工具（无该工具的模型会幻觉出
    # {"tool_calls": [{"function": {"name": "todo_list", ...}}]} 文本）
    assert "调用 todo" not in wp
    assert "todo 的 list" not in wp
    assert "检查当前 Todo（第一优先级）" not in wp
    # 改为能力描述：有工具的模型自然调用，没有的理解为上下文回顾
    assert "任务清单（Todo）" in wp
    assert "长期记忆（Memory）" in wp
    # 始终存在的交付/搜索工具保留具名指引
    assert "message_user" in wp
    assert "deliver_reply" in wp
    assert "web_search" in wp
    # 禁止段与正文措辞同步
    assert "查完任务、记忆和上下文之后什么都不发" in wp


def test_wakeup_prompt_delivery_contract_unchanged():
    import proactive

    wp = proactive.WAKEUP_PROMPT
    # 交付契约关键句必须保留（send 缺省 false / 必须真正发起 tool_calls）
    assert "send=true" in wp
    assert "tool_calls API" in wp
