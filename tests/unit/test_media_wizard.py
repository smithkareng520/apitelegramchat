"""媒体参数交互卡片（media_wizard）+ 视频请求体全 schema 回归测试。

覆盖六个层面：
1. 视频请求体全 schema（Agnes Video 2.5 文档）：keyframe 首尾帧、
   reference 的 images/audios/videos 对象（start_seconds/require_audio）、
   seed、显式 mode 与媒体匹配校验（不匹配安全回退 text）、text 模式
   绝不携带媒体字段。
2. 参数声明推导：卡片按钮按模型有效参数生成（video 全量、images inline
   尺寸/比例、chat 模型不出卡片）；n=1 只说明不提供按钮。
3. 输入清洗与附件提取（引用前缀 / 📎 占位行 / 信封兼容）。
4. 卡片渲染：主页摘要（未选择 = 模型默认）、n=1 说明、互斥警告、
   HTML 转义；picker/mode/frames/refs/seed/videoref 各页。
5. 回调状态机（Telegram IO 全部 mock）：翻页/选参/白名单拒绝/模式切换
   引导跳页/收集素材/上传失败提示重传/参考视频 start_seconds 与
   require_audio/seed 输入/文本更新提示词/提交校验与 overrides 构造。
6. 媒体循环 media_overrides：卡片参数优先于消息内提取（video seconds/
   size/aspect_ratio/mode/首尾帧/audios/video_specs；image 尺寸/比例/
   参考图直通 ImageTask）。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from config import SUPPORTED_MODELS
from core.images import ImageTaskResult
from media_wizard import (
    WIZARD_CALLBACK_PREFIX,
    MediaParamSpec,
    WizardSession,
    build_submission,
    clean_prompt_text,
    extract_media_attachments,
    get_session,
    handle_wizard_callback,
    render_page,
    resolve_media_param_spec,
    run_media_generation,
    try_consume_media_message,
    try_consume_text_message,
)
from protocols import build_video_request_body, resolve_request_plan


def _video_plan():
    return resolve_request_plan(SUPPORTED_MODELS["agnes-video-2.5"])


# ---------------------------------------------------------------------------
# 1. build_video_request_body：全文档 schema
# ---------------------------------------------------------------------------
def test_video_body_keyframe_mode_with_frames():
    body = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="人物转身走向窗边",
        seconds="5", mode="keyframe",
        first_frame="https://r2.example/first.png",
        last_frame="https://r2.example/last.png",
    )
    assert body["mode"] == "keyframe"
    assert body["first_frame"] == "https://r2.example/first.png"
    assert body["last_frame"] == "https://r2.example/last.png"
    # keyframe 禁止参考素材字段
    assert "images" not in body and "audios" not in body and "videos" not in body


def test_video_body_keyframe_single_frame_allowed():
    # 文档：first_frame 与 last_frame 至少提供其一
    body = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x", mode="keyframe",
        first_frame="https://r2.example/first.png",
    )
    assert body["mode"] == "keyframe" and body["first_frame"].endswith("first.png")
    assert "last_frame" not in body


def test_video_body_auto_mode_keyframe_wins_over_refs():
    # 未显式指定 mode：有首尾帧 -> keyframe（参考素材被忽略，符合互斥规则）
    body = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x",
        first_frame="https://r2.example/f.png",
        reference_images=("https://r2.example/a.png",),
    )
    assert body["mode"] == "keyframe"
    assert "images" not in body


def test_video_body_reference_mode_videos_object_shape():
    body = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="参考动作",
        reference_audios=("https://r2.example/a.mp3",),
        video_specs=({"url": "https://r2.example/in.mp4",
                      "start_seconds": 35, "require_audio": True},),
    )
    assert body["mode"] == "reference"
    assert body["audios"] == ["https://r2.example/a.mp3"]
    assert body["videos"] == [{"url": "https://r2.example/in.mp4",
                               "start_seconds": 35, "require_audio": True}]
    assert "<Audio 1>" in body["prompt"] and "<Video 1>" in body["prompt"]


def test_video_body_video_specs_defaults_omitted():
    # start_seconds=0 / require_audio 缺省不发送（文档默认值）
    body = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x",
        video_specs=({"url": "https://r2.example/in.mp4",
                      "start_seconds": 0, "require_audio": False},),
    )
    assert body["videos"] == [{"url": "https://r2.example/in.mp4"}]


def test_video_body_video_specs_without_url_dropped():
    body = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x",
        video_specs=({"start_seconds": 3}, "https://r2.example/ok.mp4"),
    )
    assert body["videos"] == [{"url": "https://r2.example/ok.mp4"}]


def test_video_body_explicit_text_mode_strips_media():
    # 显式 text：绝不携带任何媒体字段（文档硬约束）
    body = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x", mode="text",
        reference_images=("https://r2.example/a.png",),
    )
    assert body["mode"] == "text"
    for key in ("images", "audios", "videos", "first_frame", "last_frame"):
        assert key not in body


def test_video_body_exp_mode_without_media_falls_back_text():
    # keyframe/reference 无素材：安全回退 text，绝不发出必败请求
    body = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x", mode="keyframe",
    )
    assert body["mode"] == "text"
    body2 = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x", mode="reference",
    )
    assert body2["mode"] == "text"


def test_video_body_seed_only_integer():
    body = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x", seed=1101,
    )
    assert body["seed"] == 1101
    body2 = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x", seed="abc",
    )
    assert "seed" not in body2
    body3 = build_video_request_body(
        _video_plan(), model="agnes-video-2.5", prompt="x", seed=True,
    )
    assert "seed" not in body3


# ---------------------------------------------------------------------------
# 2. 参数声明推导（卡片按钮由模型有效参数决定）
# ---------------------------------------------------------------------------
def test_spec_video_full_buttons_n_fixed():
    spec = resolve_media_param_spec(SUPPORTED_MODELS["agnes-video-2.5"])
    assert spec.api_type == "video"
    assert spec.size_options == ("720P", "1080P", "1K", "2K")
    assert spec.ratio_options == ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
    assert spec.seconds_options == tuple(str(v) for v in range(4, 13))
    assert spec.supports_mode and spec.supports_frames and spec.supports_seed
    assert spec.max_ref_images == 8 and spec.max_ref_audios == 3 and spec.max_ref_videos == 1
    # 文档：n 仅支持 1 -> 只说明，不提供按钮
    assert any("n = 1" in note for note in spec.fixed_notes)


def test_spec_image_inline_sizes_and_ratios():
    spec = resolve_media_param_spec(SUPPORTED_MODELS["agnes-image-2.5-flash"])
    assert spec.api_type == "images"
    assert spec.size_options == ("1K", "2K", "3K", "4K")
    assert spec.ratio_options == ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")
    assert spec.supports_ref_images and not spec.supports_mode


def test_spec_chat_model_returns_none():
    assert resolve_media_param_spec(SUPPORTED_MODELS["agnes-3.0-flash"]) is None


# ---------------------------------------------------------------------------
# 3. 输入清洗与附件提取
# ---------------------------------------------------------------------------
def test_clean_prompt_text_variants():
    assert clean_prompt_text("💡 引用回复: 你好") == "你好"
    assert clean_prompt_text("📎 用户上传了图片「photo_x.jpg」\n\n日落延时") == "日落延时"
    assert clean_prompt_text("📎 用户上传了图片「p.jpg」\n\n请描述这张图片的内容") == ""
    assert clean_prompt_text("生成一段赛博朋克夜景") == "生成一段赛博朋克夜景"


def test_extract_media_attachments_envelopes():
    atts = extract_media_attachments({
        "type": "photo_group", "file_ids": ["a", "b"],
        "attachments": [{"kind": "photo", "file_id": "a"}, {"kind": "photo", "file_id": "b"}],
    })
    assert [a["file_id"] for a in atts] == ["a", "b"]
    assert extract_media_attachments({"type": "voice", "file_id": "v1"})[0]["kind"] == "voice"
    assert extract_media_attachments({"type": "video", "file_id": "v2", "mime_type": "video/mp4"})[0]["mime"] == "video/mp4"
    assert extract_media_attachments({"content": "纯文本"}) == []
    assert extract_media_attachments(None) == []


# ---------------------------------------------------------------------------
# 4. 渲染
# ---------------------------------------------------------------------------
def _video_session(**kwargs):
    spec = resolve_media_param_spec(SUPPORTED_MODELS["agnes-video-2.5"])
    prompt = kwargs.pop("prompt", "一只猫在草地上奔跑")
    return WizardSession(chat_id=100, model_id="agnes-video-2.5",
                         api_type="video", spec=spec, prompt=prompt, **kwargs)


def _image_session(**kwargs):
    spec = resolve_media_param_spec(SUPPORTED_MODELS["agnes-image-2.5-flash"])
    return WizardSession(chat_id=100, model_id="agnes-image-2.5-flash",
                         api_type="images", spec=spec, prompt="赛博朋克城市", **kwargs)


def test_render_main_video_defaults_and_n_note():
    text, kb = render_page(_video_session())
    assert "默认（5 秒）" in text and "默认（720P）" in text and "默认（16:9）" in text
    assert "n = 1（模型固定）" in text          # n 只说明不提供按钮
    data = str(kb)
    assert "mw:submit" in data and "mw:cancel" in data
    assert "mw:page:frames" in data and "mw:page:refs" in data


def test_render_main_image_page():
    text, kb = render_page(_image_session())
    assert "默认（1K）" in text and "默认（1:1）" in text
    assert "文生图" in text
    assert "mw:page:size" in str(kb)
    # 图像卡片没有模式/时长页（请求层不消费）
    assert "mw:page:seconds" not in str(kb) and "mw:page:mode" not in str(kb)


def test_render_escapes_prompt_html():
    sess = _video_session(prompt="<script>alert(1)</script> & 图片")
    text, _ = render_page(sess)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_render_mode_conflict_warning():
    sess = _video_session(first_frame="https://r2.example/f.png")
    sess.ref_images.append("https://r2.example/a.png")
    text, _ = render_page(sess)
    assert "互斥" in text and "keyframe" in text


def test_render_refs_page_video_limits():
    sess = _video_session(page="refs")
    sess.ref_videos.append({"url": "https://r2.example/in.mp4", "start_seconds": 7})
    text, kb = render_page(sess)
    assert "0/8" in text and "1/1" in text
    assert "7秒起" in str(kb)                      # 起始时间在按钮标签上
    assert "mw:vrset:1" in str(kb)
    sess.ref_videos[0]["require_audio"] = True
    text2, kb2 = render_page(sess)
    assert "必须含音轨" in str(kb2)                 # 音轨状态也在按钮标签上


# ---------------------------------------------------------------------------
# 5. 回调状态机（Telegram IO mock）
# ---------------------------------------------------------------------------
class _IO:
    """捕获 media_wizard 的 Telegram 出站调用。"""

    def __init__(self):
        self.edits: list[tuple[int, str, dict | None]] = []
        self.answers: list[tuple[str, bool]] = []

    def install(self, monkeypatch):
        import media_wizard as mw

        async def fake_edit(chat_id, message_id, text, keyboard):
            self.edits.append((message_id, text, keyboard))
            return True

        async def fake_answer(cb_id, text="", alert=False):
            self.answers.append((text, alert))

        monkeypatch.setattr(mw, "edit_card_message", fake_edit)
        monkeypatch.setattr(mw, "answer_callback", fake_answer)
        return self

    @property
    def last_text(self) -> str:
        return self.edits[-1][1] if self.edits else ""

    @property
    def last_kb(self) -> dict | None:
        return self.edits[-1][2] if self.edits else None


async def _tap(io, sess, data: str):
    await handle_wizard_callback(sess.chat_id, sess.chat_id, sess.message_id, "cb1", data)


def _register(sess: WizardSession, monkeypatch):
    import media_wizard as mw
    monkeypatch.setattr(mw, "_sessions", {sess.chat_id: sess})
    return sess


def test_callback_navigate_and_pick(monkeypatch):
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)

    run = asyncio.new_event_loop()
    run.run_until_complete(_tap(io, sess, "mw:page:size"))
    assert sess.page == "size" and "分辨率" in io.last_text
    run.run_until_complete(_tap(io, sess, "mw:set:size:1080P"))
    assert sess.size == "1080P"
    run.run_until_complete(_tap(io, sess, "mw:page:seconds"))
    run.run_until_complete(_tap(io, sess, "mw:set:seconds:8"))
    assert sess.seconds == "8"
    run.run_until_complete(_tap(io, sess, "mw:page:ratio"))
    run.run_until_complete(_tap(io, sess, "mw:set:ratio:9:16"))
    assert sess.ratio == "9:16"
    run.run_until_complete(_tap(io, sess, "mw:page:main"))
    assert "1080P" in io.last_text and "8 秒" in io.last_text and "9:16" in io.last_text
    run.close()


def test_callback_rejects_forged_values(monkeypatch):
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)
    run = asyncio.new_event_loop()
    # 回调数据伪造（白名单外的 size / 非法秒数）：拒绝且不落状态
    run.run_until_complete(_tap(io, sess, "mw:set:size:1280x720"))
    assert sess.size is None and ("无效选项", True) in io.answers
    run.run_until_complete(_tap(io, sess, "mw:set:seconds:30"))
    assert sess.seconds is None
    run.close()


def test_callback_mode_keyframe_guides_to_frames(monkeypatch):
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)
    run = asyncio.new_event_loop()
    run.run_until_complete(_tap(io, sess, "mw:set:mode:keyframe"))
    assert sess.mode == "keyframe" and sess.page == "frames"
    assert "请上传首帧" in io.answers[-1][0]
    # 依用户流程：先发首帧 -> 再发尾帧
    run.run_until_complete(_tap(io, sess, "mw:collect:first_frame"))
    assert sess.collect_slot == "first_frame"
    run.run_until_complete(_tap(io, sess, "mw:clear:first_frame"))
    assert sess.first_frame is None
    run.close()


def test_media_collect_success_and_failure_retry(monkeypatch):
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)
    sess.collect_slot = "first_frame"

    import media_wizard as mw
    run = asyncio.new_event_loop()

    # 第一次上传失败（未取得预签名 URL）-> 提示重新发送（用户要求的重传语义）
    async def fail_url(kind, file_id, mime=""):
        return ""

    monkeypatch.setattr(mw, "resolve_media_presigned_url", fail_url)
    consumed = run.run_until_complete(
        try_consume_media_message(sess.chat_id, {"type": "photo", "file_id": "f1"}))
    assert consumed and sess.first_frame is None
    assert "重新发送" in sess.collect_error

    # 重传成功 -> URL 入槽，错误提示清空
    async def ok_url(kind, file_id, mime=""):
        return f"https://r2.example/{file_id}"

    monkeypatch.setattr(mw, "resolve_media_presigned_url", ok_url)
    consumed = run.run_until_complete(
        try_consume_media_message(sess.chat_id, {"type": "photo", "file_id": "f2"}))
    assert consumed and sess.first_frame == "https://r2.example/f2"
    assert sess.collect_error is None and sess.collect_slot is None
    run.close()


def test_media_collect_mismatched_kind_consumed_with_hint(monkeypatch):
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)
    sess.collect_slot = "ref_audio"
    run = asyncio.new_event_loop()
    consumed = run.run_until_complete(
        try_consume_media_message(sess.chat_id, {"type": "photo", "file_id": "p1"}))
    assert consumed and sess.collect_slot == "ref_audio"   # 保持收集态
    assert "音频" in sess.collect_error
    run.close()


def test_media_ref_video_then_start_seconds_and_audio(monkeypatch):
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)
    sess.collect_slot = "ref_video"

    import media_wizard as mw
    run = asyncio.new_event_loop()

    async def ok_url(kind, file_id, mime=""):
        return f"https://r2.example/{file_id}.mp4"

    monkeypatch.setattr(mw, "resolve_media_presigned_url", ok_url)
    # 用户发的是视频 -> 收集后直接进入其设置页（起始时间/音轨）
    run.run_until_complete(
        try_consume_media_message(sess.chat_id, {"type": "video", "file_id": "vid", "mime_type": "video/mp4"}))
    assert sess.page == "videoref:1"
    assert "起始时间" in io.last_text

    # 点『设置起始秒数』进入输入态，再发送秒数 -> 存入 video_specs
    run.run_until_complete(_tap(io, sess, "mw:secin:1"))
    assert sess.awaiting_input == "start_seconds:1"
    consumed = run.run_until_complete(try_consume_text_message(sess.chat_id, "35"))
    assert consumed and sess.ref_videos[0]["start_seconds"] == 35

    # 音轨切换
    run.run_until_complete(_tap(io, sess, "mw:ra:1"))
    assert sess.ref_videos[0]["require_audio"] is True
    run.close()


def test_text_seed_input_and_prompt_update(monkeypatch):
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)
    run = asyncio.new_event_loop()

    run.run_until_complete(_tap(io, sess, "mw:seed:input"))
    assert sess.awaiting_input == "seed"
    run.run_until_complete(try_consume_text_message(sess.chat_id, "42"))
    assert sess.seed == 42 and sess.awaiting_input is None

    # 普通文本更新提示词
    consumed = run.run_until_complete(try_consume_text_message(sess.chat_id, "改成夜晚场景"))
    assert consumed and sess.prompt == "改成夜晚场景" and sess.page == "main"
    run.close()


def test_consume_ignores_when_no_session(monkeypatch):
    import media_wizard as mw
    monkeypatch.setattr(mw, "_sessions", {})
    run = asyncio.new_event_loop()
    assert not run.run_until_complete(
        try_consume_media_message(1, {"type": "photo", "file_id": "x"}))
    assert not run.run_until_complete(try_consume_text_message(1, "hello"))
    run.close()


def test_submit_keyframe_without_frames_blocks(monkeypatch):
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)
    sess.mode = "keyframe"
    run = asyncio.new_event_loop()
    run.run_until_complete(_tap(io, sess, "mw:submit"))
    # 校验失败：跳回 frames 页 + alert 提示，会话保留
    assert get_session(sess.chat_id) is sess
    assert "首尾帧模式需要" in io.answers[-1][0]
    run.close()


def test_submit_builds_overrides_and_finishes(monkeypatch):
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)
    sess.size = "1080P"
    sess.ratio = "21:9"
    sess.seconds = "8"
    sess.seed = 1101
    sess.first_frame = "https://r2.example/f.png"
    sess.last_frame = "https://r2.example/l.png"

    spawned: list = []

    import media_wizard as mw

    class _FakeTurnModule:
        @staticmethod
        async def spawn_turn_task(chat_id, coro):
            spawned.append((chat_id, coro))
            coro.close()   # 测试不真正执行生成
            return None

    monkeypatch.setitem(__import__("sys").modules, "app_turns", _FakeTurnModule)

    run = asyncio.new_event_loop()
    run.run_until_complete(_tap(io, sess, "mw:submit"))
    run.close()

    # keyframe 模式合法（有帧）-> 会派发生成任务（fake 已关闭 coro）
    assert len(spawned) == 1 and spawned[0][0] == sess.chat_id


def test_build_submission_video_overrides_and_defaults():
    sess = _video_session()
    sess.size = "2K"
    sess.seconds = "12"
    sess.ref_audios.append("https://r2.example/a.mp3")
    sess.ref_videos.append({"url": "https://r2.example/v.mp4", "start_seconds": 3})
    request, _, _ = build_submission(sess)
    ov = request["overrides"]
    assert ov["size"] == "2K" and ov["seconds"] == "12"
    assert ov["reference_audios"] == ["https://r2.example/a.mp3"]
    assert ov["video_specs"] == [{"url": "https://r2.example/v.mp4", "start_seconds": 3}]
    assert "mode" not in ov                     # 自动模式不显式指定
    assert "reference_images" not in ov         # 未收集任何参考图

    # 未选择任何参数 -> 空 overrides（全部走模型默认）
    req2, _, _ = build_submission(_video_session())
    assert req2["overrides"] == {}


def test_build_submission_image_overrides():
    sess = _image_session()
    sess.size = "4K"
    sess.ratio = "3:2"
    sess.ref_images.append("https://r2.example/ref.png")
    request, _, _ = build_submission(sess)
    ov = request["overrides"]
    assert ov["image_size"] == "4K" and ov["aspect_ratio"] == "3:2"
    assert ov["reference_images"] == ["https://r2.example/ref.png"]


# ---------------------------------------------------------------------------
# 6. 媒体循环 media_overrides（卡片参数优先于消息内提取）
# ---------------------------------------------------------------------------
def test_video_loop_overrides_reach_request(monkeypatch):
    import ai.agentic_loops as loops

    captured: dict = {}

    class _NullScope:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_agnes_video(prompt, duration, model, reference_images=(), reference_videos=(), **kwargs):
        captured.update(kwargs)
        captured["duration"] = duration
        captured["reference_images"] = reference_images
        return None, "stop", None

    monkeypatch.setattr(loops, "chat_action_scope", _NullScope)
    monkeypatch.setattr(loops, "_request_agnes_video", fake_agnes_video)

    from core.messages import Message, TextBlock
    messages = [Message.user([TextBlock("生成 10 秒的视频")])]
    overrides = {
        "seconds": "6", "size": "1080P", "aspect_ratio": "9:16",
        "mode": "keyframe", "seed": 7,
        "first_frame": "https://r2.example/f.png", "last_frame": "https://r2.example/l.png",
        "reference_audios": ["https://r2.example/a.mp3"],
        "video_specs": [{"url": "https://r2.example/in.mp4", "start_seconds": 5}],
    }
    run = asyncio.new_event_loop()
    raw, _, _ = run.run_until_complete(
        loops._agentic_loop_native_video("agnes-video-2.5", messages, None, 1,
                                         media_overrides=overrides))
    run.close()

    assert raw.startswith("VIDEO_ERROR")      # mock 返回错误即短路（无 IO）
    assert captured["duration"] == 6          # 卡片 seconds 优先于文本里的 "10 秒"
    assert captured["size"] == "1080P"
    assert captured["aspect_ratio"] == "9:16"
    assert captured["mode"] == "keyframe"
    assert captured["seed"] == 7
    assert captured["first_frame"].endswith("f.png")
    assert list(captured["reference_audios"]) == ["https://r2.example/a.mp3"]
    assert captured["video_specs"][0]["start_seconds"] == 5


def test_image_loop_overrides_reach_task(monkeypatch):
    import ai.agentic_loops as loops

    captured: dict = {}

    async def fake_dispatch(task):
        captured["task"] = task
        return ImageTaskResult(images=[], endpoint="/images/generations")

    monkeypatch.setattr(loops, "dispatch_image_task", fake_dispatch)

    from core.messages import Message, TextBlock
    messages = [Message.user([TextBlock("一只猫")])]
    overrides = {
        "image_size": "3K", "aspect_ratio": "21:9",
        "reference_images": ["https://r2.example/ref1.png", "https://r2.example/ref2.png"],
    }
    run = asyncio.new_event_loop()
    raw, _, _ = run.run_until_complete(
        loops._agentic_loop_native_image(None, "agnes-image-2.5-flash", messages, None, 1,
                                         media_overrides=overrides))
    run.close()

    task = captured["task"]
    assert raw.startswith("IMAGE_ERROR")      # mock 无图即报错短路（无 IO）
    assert task.image_size == "3K"
    assert task.aspect_ratio == "21:9"
    # 卡片参考图 -> edit 语义（图生图/多图合成）
    assert task.operation == "edit"
    assert len(task.input_images) == 2


# ---------------------------------------------------------------------------
# 7. 失败通知渲染与报错语义（2026-09 ModelScope 事故回归）
# ---------------------------------------------------------------------------
def test_notify_generation_failure_no_double_escape(monkeypatch):
    """修复回归：HTML notice 不得被二次转义（用户曾看到 &lt;b&gt; 字面量）。

    notice 来自 IMAGE_ERROR/VIDEO_ERROR 信号，本身是 Telegram HTML；
    _notify_generation_failure 现在复用 _render_media_failure_quote
    （与 ai_handlers 的 IMAGE_ERROR/VIDEO_ERROR 渲染完全同款 <pre> 结果块）。
    """
    import media_wizard as mw
    import turn_recovery
    import utils

    sent: dict = {}

    async def fake_send(chat_id, html_text, **kwargs):
        sent["html"] = html_text

    async def fake_mark(chat_id):
        pass

    monkeypatch.setattr(utils, "send_rich_html_message", fake_send)
    monkeypatch.setattr(turn_recovery, "mark_failed_unanswered_user", fake_mark)

    notice = "⚠️ <b>ModelScope 图像接口 请求失败</b><br/>HTTP 状态：200<br/>模型：Qwen/Qwen-Image-Edit"
    run = asyncio.new_event_loop()
    run.run_until_complete(mw._notify_generation_failure(1, notice))
    run.close()

    out = sent["html"]
    # 核心断言：不再出现转义后的标签字面量
    assert "&lt;b&gt;" not in out
    assert "&lt;br/&gt;" not in out
    # 标题文本保留（标签被剥掉，内容可见）
    assert "ModelScope 图像接口 请求失败" in out
    assert "HTTP 状态：200" in out
    # 与 IMAGE_ERROR 非卡片路径同款 <pre> 结果块渲染
    assert out.startswith("<p><b>Result</b></p><pre><code>")
    assert out.endswith("</code></pre>")


def test_notify_generation_failure_escapes_pure_text(monkeypatch):
    """纯文本 notice（含 <、& 特殊字符）也必须安全转义，不破坏 HTML 结构。"""
    import media_wizard as mw
    import turn_recovery
    import utils

    sent: dict = {}

    async def fake_send(chat_id, html_text, **kwargs):
        sent["html"] = html_text

    async def fake_mark(chat_id):
        pass

    monkeypatch.setattr(utils, "send_rich_html_message", fake_send)
    monkeypatch.setattr(turn_recovery, "mark_failed_unanswered_user", fake_mark)

    run = asyncio.new_event_loop()
    run.run_until_complete(mw._notify_generation_failure(1, "生成任务异常: <ModelScope> & quota"))
    run.close()

    out = sent["html"]
    # 既有契约：notice 走"可能含 HTML 的混合文本"净化通道——标签被剥除、
    # & 被转义，输出始终是结构安全的 <pre> 结果块（绝不输出原始尖括号）。
    assert "<ModelScope>" not in out
    assert "&amp;" in out
    assert "quota" in out
    assert out.startswith("<p><b>Result</b></p><pre><code>")
    assert out.endswith("</code></pre>")


def test_image_loop_error_reports_download_diagnostics(monkeypatch):
    """图片链接下载校验失败：报错必须说明真实原因，而非"未找到可用图片数据"。"""
    import ai.agentic_loops as loops

    async def fake_dispatch(task):
        return ImageTaskResult(
            images=[],
            endpoint="/images/generations",
            diagnostics=[
                "图片 #1（modelscope-studios.oss-cn-zhangjiakou.aliyuncs.com）："
                "链接下载了 7627 字节，但内容不是有效图片（HTML 页面（疑似防盗链/错误页））",
            ],
        )

    monkeypatch.setattr(loops, "dispatch_image_task", fake_dispatch)

    from core.messages import Message, TextBlock
    messages = [Message.user([TextBlock("一只猫")])]
    run = asyncio.new_event_loop()
    raw, _, _ = run.run_until_complete(
        loops._agentic_loop_native_image(None, "agnes-image-2.5-flash", messages, None, 1))
    run.close()

    assert raw.startswith("IMAGE_ERROR")
    notice = raw.split(":", 1)[1]
    # 语义准确：说明"下载/校验失败" + 诊断行 + 建议，不再误报"没有数据"
    assert "下载/校验失败" in notice
    assert "7627" in notice
    assert "防盗链" in notice
    assert "未找到可用图片数据" not in notice
    # HTML notice 无转义残留
    assert "&lt;" not in notice and "&gt;" not in notice


# ---------------------------------------------------------------------------
# 8. 提交卡片终态就地更新（修复：卡片提交后停在"进行中"不再变化）
# ---------------------------------------------------------------------------
def test_submit_card_shows_in_progress_not_done(monkeypatch):
    """提交瞬间卡片文案必须是"进行中"语义，不能读起来像已经生成完成。"""
    io = _IO().install(monkeypatch)
    sess = _register(_video_session(message_id=55), monkeypatch)

    import media_wizard as mw

    class _FakeTurnModule:
        @staticmethod
        async def spawn_turn_task(chat_id, coro):
            coro.close()   # 本测试只关心提交瞬间的卡片文案，不跑生成
            return None

    monkeypatch.setitem(__import__("sys").modules, "app_turns", _FakeTurnModule)

    run = asyncio.new_event_loop()
    run.run_until_complete(_tap(io, sess, "mw:submit"))
    run.close()

    assert "生成完成" not in io.last_text
    assert "已提交" not in io.last_text or "进行中" in io.last_text or "正在生成" in io.last_text
    assert "正在生成" in io.last_text


def test_run_media_generation_finalizes_card_on_success(monkeypatch):
    """生成成功后必须就地把提交卡片编辑为终态，而不是留在"进行中"。"""
    io = _IO().install(monkeypatch)
    sess = _video_session(message_id=55)
    request, _, _ = build_submission(sess)
    request["message_id"] = sess.message_id

    import ai.agentic_loops as loops
    import app_turns

    async def fake_video(*a, **k):
        return "VIDEO_SENT", None, []

    async def fake_ledger(*a, **k):
        return None

    monkeypatch.setattr(loops, "_agentic_loop_native_video", fake_video)
    monkeypatch.setattr(app_turns, "update_conversation_and_ledger", fake_ledger)

    run = asyncio.new_event_loop()
    run.run_until_complete(run_media_generation(sess.chat_id, request))
    run.close()

    assert io.edits, "生成成功后应至少编辑一次卡片"
    final_message_ids = [mid for mid, _, _ in io.edits]
    assert sess.message_id in final_message_ids
    finalized_text = next(text for mid, text, _ in io.edits if mid == sess.message_id)
    assert "生成完成" in finalized_text
    assert "进行中" not in finalized_text


def test_run_media_generation_finalizes_card_on_failure(monkeypatch):
    """生成失败也必须把提交卡片编辑为失败终态（而不是只发一条不相关的新消息）。"""
    io = _IO().install(monkeypatch)
    sess = _video_session(message_id=55)
    request, _, _ = build_submission(sess)
    request["message_id"] = sess.message_id

    import ai.agentic_loops as loops
    import turn_recovery
    import utils

    async def fake_video(*a, **k):
        return "VIDEO_ERROR:配额不足", None, []

    async def fake_send(chat_id, html_text, **kwargs):
        pass

    async def fake_mark(chat_id):
        pass

    monkeypatch.setattr(loops, "_agentic_loop_native_video", fake_video)
    monkeypatch.setattr(utils, "send_rich_html_message", fake_send)
    monkeypatch.setattr(turn_recovery, "mark_failed_unanswered_user", fake_mark)

    run = asyncio.new_event_loop()
    run.run_until_complete(run_media_generation(sess.chat_id, request))
    run.close()

    assert io.edits, "生成失败后应至少编辑一次卡片"
    finalized_text = next(text for mid, text, _ in io.edits if mid == sess.message_id)
    assert "生成失败" in finalized_text
    assert "配额不足" in finalized_text
    assert "进行中" not in finalized_text
