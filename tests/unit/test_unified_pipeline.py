"""统一请求管道回归测试（参数分层 / 输入组合鉴权 / API 分支 / 请求体构建）。

验证目标：回合入口的"一个用户回合到底怎么发出去"四步流水线全部配置
驱动——厂商默认参数 -> 模型覆盖参数的统一合并视图（resolve_effective_params）、
按输入组合 × 模型能力的统一鉴权出口（authorize_request）、chat/images/video
分支解析（resolve_request_plan）、媒体请求体按（内容, api 类型, 端点）装配
（build_media_request_body）。

覆盖五个层面：
1. 参数分层：模型覆盖 > 厂商默认（openrouter/free 示例：vision=True
   覆盖厂商默认 False；未覆盖字段继承厂商默认）；端点合并同规则。
2. 输入组合解析：单一类型信封 / 混合 attachments 信封 / 空信封。
3. 鉴权：支持模态 ok；不支持模态降级（degrade 记录原因，不阻断）；
   媒体分支缺 prompt 硬拦截（blocked）；chat 分支永不因缺文本拦截。
4. 分支计划：三类模型 -> chat/images/video + 协议 + 端点 + 图像形状。
5. 请求体构建：inline（Agnes 式）硬约束不回归；multipart/chat/video
   分支不适用 JSON 构建（返回 None）。
"""
import json

import pytest

from config import (
    PROVIDERS,
    SUPPORTED_MODELS,
    make_model_config,
    resolve_effective_params,
)
from protocols import (
    authorize_request,
    build_media_request_body,
    resolve_input_combination,
    resolve_request_plan,
    run_preflight,
)

AGNES_IMAGES_URL = "https://apihub.agnes-ai.com/v1/images/generations"
AGNES_VIDEOS_URL = "https://apihub.agnes-ai.com/v1/videos"


# ---------------------------------------------------------------------------
# 1. 参数分层：厂商默认 -> 模型覆盖
# ---------------------------------------------------------------------------

def test_model_override_beats_provider_default():
    # 用户示例模型：openrouter/free 显式声明 vision=True / supports_tools=False
    # / reasoning_effort="high" / max_context=200000，全部应覆盖厂商默认
    # （openrouter 默认 vision=False / supports_tools=True / max_context=128000）。
    params = resolve_effective_params(SUPPORTED_MODELS["openrouter/free"])
    assert params.provider == "openrouter"
    assert params.model_id == "openrouter/free"
    assert params.vision is True          # 模型覆盖（厂商默认 False）
    assert params.supports_tools is False  # 模型覆盖（厂商默认 True）
    assert params.reasoning_effort == "high"
    assert params.max_context == 200000
    # 未覆盖字段继承厂商默认
    assert params.audio is False
    assert params.native_image is False


def test_unspecified_fields_inherit_provider_defaults():
    # 不做任何覆盖的模型：全部继承厂商默认
    cfg = make_model_config(
        model_id="plain-model", provider="openrouter", name="Plain",
    )
    params = resolve_effective_params(cfg)
    assert params.vision is False
    assert params.supports_tools is True
    assert params.max_context == 128000
    assert params.max_output_tokens == 65536


def test_endpoint_layering_provider_default_then_model_override():
    # 端点分层：模型未声明 -> 厂商默认（openrouter 无端点声明，按协议推导）；
    # 模型声明完整端点 -> 模型覆盖生效。
    plain = make_model_config(
        model_id="plain-model", provider="openrouter", name="Plain",
    )
    p = resolve_effective_params(plain)
    assert p.endpoint.endpoint is None
    assert p.endpoint.base_url == PROVIDERS["openrouter"].base_url

    agnes_img = resolve_effective_params(SUPPORTED_MODELS["agnes-image-2.5-flash"])
    assert agnes_img.endpoint.endpoint == AGNES_IMAGES_URL
    assert agnes_img.endpoint.protocol == "openai_images"
    # inline 形状来自厂商级声明（agnes: image_edit_inline=True）
    assert agnes_img.endpoint.image_edit_inline is True


def test_capability_for_modality_mapping():
    params = resolve_effective_params(SUPPORTED_MODELS["openrouter/free"])
    assert params.capability_for_modality("photo") is True
    assert params.capability_for_modality("image") is True
    assert params.capability_for_modality("audio") is False
    assert params.capability_for_modality("video") is False
    assert params.capability_for_modality("document") is False
    # 未知模态保守 False
    assert params.capability_for_modality("sticker") is False


def test_none_model_returns_safe_empty_params():
    params = resolve_effective_params(None)
    assert params.model_id == ""
    assert params.vision is False
    assert params.supports_tools is False


# ---------------------------------------------------------------------------
# 2. 输入组合解析
# ---------------------------------------------------------------------------

def test_combination_plain_text():
    c = resolve_input_combination({"content": "你好"})
    assert c.text == "你好"
    assert not c.has_attachments
    assert c.modalities == frozenset()


def test_combination_photo_group_envelope():
    c = resolve_input_combination({
        "type": "photo_group", "file_ids": ["a", "b", "c"], "content": "这是什么",
    })
    assert c.photo_count == 3
    assert c.text == "这是什么"


def test_combination_single_photo_envelope():
    c = resolve_input_combination({"type": "photo", "file_id": "x", "content": ""})
    assert c.photo_count == 1


def test_combination_audio_video_document_envelopes():
    assert resolve_input_combination({"type": "voice", "file_id": "v"}).audio_count == 1
    assert resolve_input_combination({"type": "video_group", "file_ids": ["1", "2"]}).video_count == 2
    assert resolve_input_combination({"type": "document", "file_id": "d"}).document_count == 1


def test_combination_mixed_attachments_envelope():
    c = resolve_input_combination({
        "content": "看",
        "attachments": [
            {"kind": "photo", "file_id": "p1"},
            {"kind": "video", "file_id": "v1"},
            {"kind": "audio", "file_id": "a1"},
            {"kind": "document", "file_id": "d1"},
        ],
    })
    assert c.photo_count == 1 and c.video_count == 1
    assert c.audio_count == 1 and c.document_count == 1
    assert c.modalities == frozenset({"photo", "audio", "video", "document"})


def test_combination_none_and_non_string_content():
    assert resolve_input_combination(None).text == ""
    assert resolve_input_combination({"content": None}).text == ""


# ---------------------------------------------------------------------------
# 3. 鉴权：输入组合 vs 模型能力
# ---------------------------------------------------------------------------

def test_auth_text_and_photo_to_vision_model_ok():
    # 用户示例场景：openrouter/free vision=True，用户输入文本+图片 -> 鉴权通过
    combo = resolve_input_combination({
        "type": "photo_group", "file_ids": ["a", "b"], "content": "这是什么",
    })
    verdict = authorize_request(SUPPORTED_MODELS["openrouter/free"], combo)
    assert verdict.ok is True
    assert verdict.blocked is False
    assert verdict.degraded == {}
    assert verdict.route == "chat"


def test_auth_photo_to_non_vision_model_degrades():
    # GLM-4.7-Flash 继承厂商默认 vision=False：图片降级文本占位，回合继续
    combo = resolve_input_combination({"type": "photo", "file_id": "x", "content": "看图"})
    verdict = authorize_request(SUPPORTED_MODELS["GLM-4.7-Flash"], combo)
    assert verdict.ok is True           # 降级不阻断
    assert verdict.degraded.get("photo")  # 有降级原因
    assert "photo" in verdict.degraded


def test_auth_mixed_modalities_partial_degrade():
    # 图片可收（vision=True）、音频不支持（audio=False）：部分降级
    combo = resolve_input_combination({
        "content": "处理一下",
        "attachments": [
            {"kind": "photo", "file_id": "p"},
            {"kind": "voice", "file_id": "a"},
        ],
    })
    verdict = authorize_request(SUPPORTED_MODELS["openrouter/free"], combo)
    assert verdict.ok is True
    assert "photo" not in verdict.degraded
    assert "audio" in verdict.degraded


def test_auth_media_route_without_prompt_blocked():
    # 生图模型收到纯图片无文本：媒体循环提取不到 prompt，上游必 400 -> 硬拦截
    combo = resolve_input_combination({"type": "photo", "file_id": "x", "content": ""})
    verdict = authorize_request(SUPPORTED_MODELS["agnes-image-2.5-flash"], combo)
    assert verdict.blocked is True
    assert verdict.block_reason  # 给出可操作的提示


def test_auth_media_route_with_prompt_ok():
    combo = resolve_input_combination({"type": "photo", "file_id": "x", "content": "把背景换成夕阳"})
    verdict = authorize_request(SUPPORTED_MODELS["agnes-image-2.5-flash"], combo)
    assert verdict.blocked is False
    assert verdict.route == "image"
    # 纯文本生视频同理
    vcombo = resolve_input_combination({"content": "生成一段5秒的城市夜景视频"})
    vverdict = authorize_request(SUPPORTED_MODELS["agnes-video-2.5"], vcombo)
    assert vverdict.route == "video"
    assert vverdict.blocked is False


def test_auth_chat_route_never_blocked_by_missing_text():
    # chat 分支：纯附件 / 空消息永不 blocked（TIMER 合成唤醒、纯图提问均合法）
    combo = resolve_input_combination({"type": "photo", "file_id": "x", "content": ""})
    verdict = authorize_request(SUPPORTED_MODELS["openrouter/free"], combo)
    assert verdict.blocked is False
    empty = authorize_request(SUPPORTED_MODELS["agnes-3.0-flash"], resolve_input_combination(None))
    assert empty.blocked is False


# ---------------------------------------------------------------------------
# 4. 分支计划：chat / images / video
# ---------------------------------------------------------------------------

def test_plan_chat_model():
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-3.0-flash"])
    assert plan.route == "chat"
    assert plan.api_type == "chat"
    assert plan.protocol == "openai_chat"
    assert plan.endpoint is None  # 无端点声明 -> 协议按 base_url 推导
    assert plan.base_url == PROVIDERS["agnes"].base_url


def test_plan_image_model_uses_declared_endpoint_and_inline_shape():
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-image-2.5-flash"])
    assert plan.route == "image"
    assert plan.api_type == "images"
    assert plan.protocol == "openai_images"
    assert plan.endpoint == AGNES_IMAGES_URL
    assert plan.image_style == "inline_images"


def test_plan_video_model_uses_declared_endpoint():
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-video-2.5"])
    assert plan.route == "video"
    assert plan.api_type == "video"
    assert plan.endpoint == AGNES_VIDEOS_URL


def test_plan_openrouter_chat_modalities_image_model():
    # OpenRouter gemini 图像模型：无端点声明，协议 openai_chat -> images 分支
    plan = resolve_request_plan(SUPPORTED_MODELS["google/gemini-3-pro-image-preview"])
    assert plan.route == "image"
    assert plan.api_type == "images"
    assert plan.protocol == "openai_chat"
    assert plan.endpoint is None


def test_plan_none_model_falls_back_to_chat():
    plan = resolve_request_plan(None)
    assert plan.route == "chat"
    assert plan.api_type == "chat"


# ---------------------------------------------------------------------------
# 5. 请求体构建：按（内容，api 类型，端点/形状）装配
# ---------------------------------------------------------------------------

def test_build_body_inline_txt2img_uses_return_base64():
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-image-2.5-flash"])
    body = build_media_request_body(
        plan, model="agnes-image-2.5-flash", prompt="一只猫",
        size="2K", ratio="16:9",
    )
    assert body == {
        "model": "agnes-image-2.5-flash",
        "prompt": "一只猫",
        "size": "2K",
        "ratio": "16:9",
        "return_base64": True,
    }


def test_build_body_inline_img2img_constraints():
    # Agnes 文档硬约束：参考图进 extra_body.image；response_format 只进
    # extra_body；不传 tags。
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-image-2.5-flash"])
    body = build_media_request_body(
        plan, model="agnes-image-2.5-flash", prompt="把物体变成橙色",
        size="1024x768", ratio="1:1",
        reference_images=("https://example.com/in.png", "data:image/png;base64,AAAA"),
    )
    assert body["extra_body"]["image"] == ["https://example.com/in.png", "data:image/png;base64,AAAA"]
    assert body["extra_body"]["response_format"] == "b64_json"
    assert "tags" not in body
    assert "response_format" not in body  # 绝不在顶层
    assert "return_base64" not in body    # 图生图路径不需要


def test_build_body_non_json_shapes_return_none():
    # multipart 编辑形状 / chat / video 分支：JSON 构建不适用
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-3.0-flash"])
    assert build_media_request_body(plan, model="x", prompt="y") is None
    vplan = resolve_request_plan(SUPPORTED_MODELS["agnes-video-2.5"])
    assert build_media_request_body(vplan, model="x", prompt="y") is None
    # multipart 形状：构造一个 inline=False 的图像模型
    cfg = make_model_config(
        model_id="official-style-image", provider="agnes", name="Official",
        native_image=True, vision=True, supports_tools=False,
        protocol="openai_images", image_edit_inline=False,
    )
    mplan = resolve_request_plan(cfg)
    assert mplan.image_style == "multipart_edits"
    assert build_media_request_body(mplan, model="x", prompt="y") is None


# ---------------------------------------------------------------------------
# 6. run_preflight：回合入口的一次性组合
# ---------------------------------------------------------------------------

def test_run_preflight_end_to_end_vision_turn():
    pf = run_preflight(SUPPORTED_MODELS["openrouter/free"], {
        "type": "photo_group", "file_ids": ["a"], "content": "这是什么",
    })
    assert pf.params.vision is True
    assert pf.combination.photo_count == 1
    assert pf.verdict.ok is True and pf.verdict.blocked is False
    assert pf.plan.api_type == "chat"
    # 日志摘要包含四步结论
    text = pf.describe()
    assert "openrouter/free" in text and "photo×1" in text and "route=chat" in text


def test_run_preflight_media_turn_blocked_short_circuit_shape():
    pf = run_preflight(SUPPORTED_MODELS["agnes-image-2.5-flash"], {
        "type": "photo", "file_id": "x", "content": "",
    })
    assert pf.verdict.blocked is True
    assert pf.plan.route == "image"
    # blocked 短路信号映射（ai_handlers 消费）：image -> IMAGE_ERROR
    sig = "VIDEO_ERROR" if pf.plan.route == "video" else "IMAGE_ERROR"
    assert sig == "IMAGE_ERROR"


def test_run_preflight_timer_empty_message_chat_route():
    pf = run_preflight(SUPPORTED_MODELS["agnes-3.0-flash"], None)
    assert pf.combination.summary() == "empty"
    assert pf.verdict.blocked is False
    assert pf.plan.route == "chat"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
