# tests/unit/test_photo_group_partial_failure.py
"""回归测试：photo_group 部分图片解析失败时不应静默丢图。

Bug 背景：用户在一条消息里发送 2 张图片（或 Telegram 相册聚合出的
photo_group），其中 1 张因 R2 预签名失败 + base64 兜底也拿不到字节而
解析失败。修复前 _resolve_multimodal_content 会直接把失败的图片从
content_blocks 过滤掉，不留任何日志或提示，导致模型误以为用户只发了
1 张图（表现为模型回复"我只看到 1 张图片"）。

修复后：
  1. 失败的 file_id 记 warning 日志；
  2. 部分失败时注入一条文本提示，让模型如实告知用户，而不是当作
     用户没发。
"""
import logging

import pytest

from config import ModelConfig
from core.messages import ImageBlock, TextBlock
import ai.attachment_content as attachment_content


def _model_info(**overrides) -> ModelConfig:
    base = dict(
        model_id="test-model",
        provider="test",
        image_input=True,
    )
    base.update(overrides)
    return ModelConfig(**base)


@pytest.mark.asyncio
async def test_photo_group_partial_failure_warns_and_notifies_model(monkeypatch, caplog):
    """2 张图，1 张解析失败：应保留 1 张图片块 + 警告文本块，且记录 warning 日志。"""

    async def fake_build_image_block(chat_id, file_id):
        if file_id == "good_fid":
            return ImageBlock(url="https://example.com/good.jpg", detail="high")
        return None  # 模拟 R2 预签名失败 + base64 兜底也失败

    monkeypatch.setattr(attachment_content, "_build_image_block", fake_build_image_block)

    async def fake_resolve_presigned_attachment_url(fid):
        return ""

    monkeypatch.setattr(
        attachment_content,
        "_resolve_presigned_attachment_url",
        fake_resolve_presigned_attachment_url,
    )

    msg = {
        "role": "user",
        "content": "这两个呢",
        "type": "photo_group",
        "file_ids": ["good_fid", "bad_fid"],
    }

    with caplog.at_level(logging.WARNING):
        blocks = await attachment_content._resolve_multimodal_content(
            msg, _model_info(), chat_id=123
        )

    # 图片块：只应有 1 个（成功的那张），不应整体丢弃或误报为 2 个。
    image_blocks = [b for b in blocks if isinstance(b, ImageBlock)]
    assert len(image_blocks) == 1
    assert image_blocks[0].url == "https://example.com/good.jpg"

    # 应该有一条警告文本块，明确告知模型有图片加载失败，避免模型
    # 误判"用户只发了这些图片"。
    text_blocks = [b for b in blocks if isinstance(b, TextBlock)]
    warning_texts = [b.text for b in text_blocks if "加载失败" in b.text]
    assert warning_texts, f"expected a partial-failure notice, got blocks={blocks}"
    assert "2 张" in warning_texts[0]
    assert "1 张" in warning_texts[0]

    # 用户原始文本仍应保留。
    assert any(b.text == "这两个呢" for b in text_blocks)

    # 失败必须留痕：不能像修复前一样完全静默。
    assert any("图片解析失败" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_photo_group_all_success_no_warning(monkeypatch, caplog):
    """全部图片解析成功时，不应出现警告文本块或 warning 日志（无回归）。"""

    async def fake_build_image_block(chat_id, file_id):
        return ImageBlock(url=f"https://example.com/{file_id}.jpg", detail="high")

    monkeypatch.setattr(attachment_content, "_build_image_block", fake_build_image_block)

    async def fake_resolve_presigned_attachment_url(fid):
        return ""

    monkeypatch.setattr(
        attachment_content,
        "_resolve_presigned_attachment_url",
        fake_resolve_presigned_attachment_url,
    )

    msg = {
        "role": "user",
        "content": "看看这两张",
        "type": "photo_group",
        "file_ids": ["fid_a", "fid_b"],
    }

    with caplog.at_level(logging.WARNING):
        blocks = await attachment_content._resolve_multimodal_content(
            msg, _model_info(), chat_id=123
        )

    image_blocks = [b for b in blocks if isinstance(b, ImageBlock)]
    assert len(image_blocks) == 2

    text_blocks = [b for b in blocks if isinstance(b, TextBlock)]
    assert not any("加载失败" in b.text for b in text_blocks)
    assert not any("图片解析失败" in rec.message for rec in caplog.records)
