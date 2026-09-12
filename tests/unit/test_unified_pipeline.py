"""统一请求管道回归测试（参数分层 / 输入组合鉴权 / API 分支 / 请求体构建）。

验证目标：回合入口的"一个用户回合到底怎么发出去"四步流水线全部配置
驱动——厂商默认参数 -> 模型覆盖参数的统一合并视图（resolve_effective_params）、
按输入组合 × 模型能力的统一鉴权出口（authorize_request）、chat/images/video
分支解析（resolve_request_plan）、媒体请求体按（内容, api 类型, 端点）装配
（build_media_request_body）。

覆盖五个层面：
1. 参数分层：模型覆盖 > 厂商默认（openrouter/free 示例：image_input=True
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
    build_video_request_body,
    normalize_video_seconds,
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
    # 用户示例模型：openrouter/free 显式声明 image_input=True / supports_tools=False
    # / reasoning_effort="high" / max_context=200000，全部应覆盖厂商默认
    # （openrouter 默认 image_input=False / supports_tools=True / max_context=128000）。
    params = resolve_effective_params(SUPPORTED_MODELS["openrouter/free"])
    assert params.provider == "openrouter"
    assert params.model_id == "openrouter/free"
    assert params.image_input is True   # 模型覆盖（厂商默认 False）
    assert params.supports_tools is False  # 模型覆盖（厂商默认 True）
    assert params.reasoning_effort == "high"
    assert params.max_context == 200000
    # 未覆盖字段继承厂商默认
    assert params.audio_input is False
    assert params.image_output is False


def test_unspecified_fields_inherit_provider_defaults():
    # 不做任何覆盖的模型：全部继承厂商默认
    cfg = make_model_config(
        model_id="plain-model", provider="openrouter", name="Plain",
    )
    params = resolve_effective_params(cfg)
    assert params.image_input is False
    assert params.supports_tools is True
    assert params.max_context == 128000
    assert params.max_output_tokens == 65536


def test_endpoint_layering_provider_default_then_model_override():
    # 端点分层：模型未声明 -> 厂商默认 endpoint（openrouter 为 API 根，
    # SDK 客户端自拼标准路径）；模型声明完整端点 -> 模型覆盖生效。
    plain = make_model_config(
        model_id="plain-model", provider="openrouter", name="Plain",
    )
    p = resolve_effective_params(plain)
    assert p.endpoint.endpoint == PROVIDERS["openrouter"].endpoint

    agnes_img = resolve_effective_params(SUPPORTED_MODELS["agnes-image-2.5-flash"])
    assert agnes_img.endpoint.endpoint == AGNES_IMAGES_URL
    assert agnes_img.endpoint.protocol == "openai_images"


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
    assert params.image_input is False
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


def test_combination_group_envelope_file_ids_and_attachments_dedup():
    # 组信封同时携带 file_ids 数组与 attachments 列表（同一批文件的两份
    # 视图）：按 file_id 去重后计数，单图 = ×1、双图相册 = ×2。
    # 2026-09-12 生产案例：单图被数成 photo×2，误导"两张图都进了管道"
    # 的排查方向（真实丢图点在信封构造之前的回复引用降级）。
    single = resolve_input_combination({
        "type": "photo_group", "file_ids": ["a"],
        "attachments": [{"kind": "photo", "file_id": "a"}],
        "content": "x",
    })
    assert single.photo_count == 1

    album = resolve_input_combination({
        "type": "photo_group", "file_ids": ["a", "b"],
        "attachments": [{"kind": "photo", "file_id": "a"},
                        {"kind": "photo", "file_id": "b"}],
        "content": "x",
    })
    assert album.photo_count == 2
    assert "photo×2" in album.summary()


def test_combination_preflight_describe_matches_real_turn():
    # 复现 2026-09-12 [6c15a092] 的真实信封（单图 + 引用回复文本 35 字符）：
    # 预检摘要应显示 photo×1（修复前误报 photo×2）。
    env = {
        "type": "photo_group", "file_ids": ["f1"],
        "attachments": [{"kind": "photo", "file_id": "f1"}],
        "content": "📎 用户引用了图片\n\n💡 引用回复:\n> [图片，无文字说明]\n\n回答",
    }
    pf = run_preflight(SUPPORTED_MODELS["agnes-3.0-flash"], env)
    assert pf.combination.summary() == "text=35字符+photo×1"


# ---------------------------------------------------------------------------
# 3. 鉴权：输入组合 vs 模型能力
# ---------------------------------------------------------------------------

def test_auth_text_and_photo_to_vision_model_ok():
    # 用户示例场景：openrouter/free image_input=True，用户输入文本+图片 -> 鉴权通过
    combo = resolve_input_combination({
        "type": "photo_group", "file_ids": ["a", "b"], "content": "这是什么",
    })
    verdict = authorize_request(SUPPORTED_MODELS["openrouter/free"], combo)
    assert verdict.ok is True
    assert verdict.blocked is False
    assert verdict.degraded == {}
    assert verdict.route == "chat"


def test_auth_photo_to_non_vision_model_degrades():
    # GLM-4.7-Flash 继承厂商默认 image_input=False：图片降级文本占位，回合继续
    combo = resolve_input_combination({"type": "photo", "file_id": "x", "content": "看图"})
    verdict = authorize_request(SUPPORTED_MODELS["GLM-4.7-Flash"], combo)
    assert verdict.ok is True           # 降级不阻断
    assert verdict.degraded.get("photo")  # 有降级原因
    assert "photo" in verdict.degraded


def test_auth_mixed_modalities_partial_degrade():
    # 图片可收（image_input=True）、音频不支持（audio_input=False）：部分降级
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
    # 无模型级覆盖 -> 端点为厂商 API 根（协议按它自拼标准路径）
    assert plan.endpoint == PROVIDERS["agnes"].endpoint


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
    # 端点为厂商 API 根（请求走 chat modalities，不经图像端点）
    assert plan.endpoint == PROVIDERS["openrouter"].endpoint


def test_plan_none_model_falls_back_to_chat():
    plan = resolve_request_plan(None)
    assert plan.route == "chat"
    assert plan.api_type == "chat"


# ---------------------------------------------------------------------------
# 7. 视频请求体构建（Agnes Video 2.5 文档 schema）
# ---------------------------------------------------------------------------

def test_normalize_video_seconds_clamps_to_doc_range():
    # 文档：seconds 为字符串 "4"–"12"，默认 "5"
    assert normalize_video_seconds(5) == "5"
    assert normalize_video_seconds("7") == "7"
    assert normalize_video_seconds(3) == "4"       # 下界钳制
    assert normalize_video_seconds(15) == "12"     # 上界钳制
    assert normalize_video_seconds(None) == "5"    # 默认值
    assert normalize_video_seconds("abc") == "5"   # 非数字回退


def test_build_video_body_text_mode_schema():
    # 纯文本 -> mode=text；时长字段是字符串 seconds（发 duration 会被
    # 网关 400 "duration is not an allowed request field"，2026-09-11 生产事故）
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-video-2.5"])
    body = build_video_request_body(
        plan, model="agnes-video-2.5", prompt="日落延时", seconds="5",
    )
    assert body == {
        "model": "agnes-video-2.5",
        "prompt": "日落延时",
        "seconds": "5",
        "mode": "text",
    }
    assert "duration" not in body
    assert "images" not in body and "videos" not in body  # text 模式禁止媒体字段
    assert "n" not in body  # 仅支持 1，不发送


def test_build_video_body_size_ratio_whitelist():
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-video-2.5"])
    body = build_video_request_body(
        plan, model="agnes-video-2.5", prompt="x",
        size="1080p", aspect_ratio="9:16",
    )
    assert body["size"] == "1080P"      # 大小写归一
    assert body["aspect_ratio"] == "9:16"
    # 白名单外（像素尺寸 / 非法画幅 / auto）一律不发送，走网关默认
    body2 = build_video_request_body(
        plan, model="agnes-video-2.5", prompt="x",
        size="1280x720", aspect_ratio="2:3",
    )
    assert "size" not in body2 and "aspect_ratio" not in body2


def test_build_video_body_reference_mode_with_placeholders():
    # 带参考媒体 -> mode=reference；图片进 images 数组、视频进 videos
    # 对象数组；prompt 追加 <Picture N>/<Video N> 占位符说明（文档建议）
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-video-2.5"])
    body = build_video_request_body(
        plan, model="agnes-video-2.5", prompt="让角色跑起来",
        reference_images=("https://r2.example/a.png", "https://r2.example/b.png"),
        reference_videos=("https://r2.example/motion.mp4",),
    )
    assert body["mode"] == "reference"
    assert body["images"] == ["https://r2.example/a.png", "https://r2.example/b.png"]
    assert body["videos"] == [{"url": "https://r2.example/motion.mp4"}]
    assert "<Picture 1>" in body["prompt"]
    assert "<Picture 2>" in body["prompt"]
    assert "<Video 1>" in body["prompt"]


def test_build_video_body_reference_media_caps():
    # 文档限制：图片最多 8 张、参考视频最多 1 个（超出截断而非让网关 400）
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-video-2.5"])
    urls = tuple(f"https://r2.example/{i}.png" for i in range(10))
    body = build_video_request_body(
        plan, model="agnes-video-2.5", prompt="x",
        reference_images=urls,
        reference_videos=("https://r2.example/1.mp4", "https://r2.example/2.mp4"),
    )
    assert len(body["images"]) == 8
    assert len(body["videos"]) == 1


def test_build_video_body_non_video_plan_returns_none():
    plan = resolve_request_plan(SUPPORTED_MODELS["agnes-3.0-flash"])
    assert build_video_request_body(plan, model="x", prompt="y") is None


# ---------------------------------------------------------------------------
# 6. run_preflight：回合入口的一次性组合
# ---------------------------------------------------------------------------

def test_run_preflight_end_to_end_vision_turn():
    pf = run_preflight(SUPPORTED_MODELS["openrouter/free"], {
        "type": "photo_group", "file_ids": ["a"], "content": "这是什么",
    })
    assert pf.params.image_input is True
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
