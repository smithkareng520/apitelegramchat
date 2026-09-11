"""配置驱动端点路由回归测试（Agnes Image 2.5 Flash 接入重构）。

验证目标：新增/接入一个走不同子端点、不同图像 API 形状的模型，只需要
在 config.py 的模型/厂商配置里声明字段（protocol / endpoint），请求层
自动按配置路由——不需要为任何模型或厂商新建请求分支。

覆盖五个层面：
1. 配置合并：模型声明的完整端点/协议正确落到有效端点；endpoint 指向
   /images/generations|edits 时按完整图像端点处理（内联形状），指向
   API 根时按官方形状推导（multipart 编辑）。
2. 公共路由：resolve_model_route 按能力字段匹配 文本/视频/生图。
3. 图像适配器解析：协议正确时直接路由；协议缺失但声明了 images 端点
   时配置驱动回退；两者皆无时明确报错。
4. inline（Agnes 式）请求形状：端点/尺寸档位/宽高比/return_base64/
   extra_body.image/response_format 位置/tags 缺失等文档硬约束。
5. multipart（OpenAI 官方式）行为不回归 + 视频提交端点配置化。
"""
import asyncio
import base64
import json

import pytest

import config as app_config
from config import SUPPORTED_MODELS, get_effective_endpoint, make_model_config
from core.images import ImageTask
from protocols import resolve_model_route
from protocols.images import (
    IMAGE_PROTOCOLS,
    dispatch_image_task,
    resolve_image_adapter,
)
from ai.media_generation import (
    _normalize_inline_ratio,
    _normalize_inline_size,
    _request_openai_compat_image,
    _request_agnes_video,
    build_inline_images_payload,
    resolve_images_endpoint_shape,
)


# 1x1 透明 PNG（有效图片，用于参考图校验路径）。
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_1X1_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_1X1).decode("ascii")

AGNES_IMAGES_URL = "https://apihub.agnes-ai.com/v1/images/generations"


# ---------------------------------------------------------------------------
# 1. 配置合并
# ---------------------------------------------------------------------------

def test_agnes_image_25_declares_images_protocol_and_endpoint():
    cfg = SUPPORTED_MODELS["agnes-image-2.5-flash"]
    ep = get_effective_endpoint(cfg)
    assert ep.protocol == "openai_images"
    # 模型配置一侧声明的完整端点原样生效
    assert ep.endpoint == AGNES_IMAGES_URL
    # 完整图像端点（指向 /images/generations）-> 内联形状（生成/编辑/多图
    # 合成共用同一端点，Agnes 形状）
    shape = resolve_images_endpoint_shape(cfg)
    assert shape.edit_inline is True
    assert shape.style == "inline_images"
    assert cfg.image_output is True


def test_agnes_image_21_shares_same_shape():
    cfg = SUPPORTED_MODELS["agnes-image-2.1-flash"]
    ep = get_effective_endpoint(cfg)
    assert ep.protocol == "openai_images"
    assert ep.endpoint == AGNES_IMAGES_URL
    shape = resolve_images_endpoint_shape(cfg)
    assert shape.edit_inline is True


def test_no_declared_endpoint_derives_official_shape():
    # endpoint 沿用厂商 API 根（未声明完整图像端点）-> OpenAI 官方形状推导：
    # {endpoint}/images/{generations,edits}，编辑走独立 multipart /images/edits。
    cfg = make_model_config(
        model_id="test-official-style",
        provider="agnes",
        name="Official Style Test",
        image_output=True,
        protocol="openai_images",
    )
    shape = resolve_images_endpoint_shape(cfg)
    assert shape.edit_inline is False
    assert shape.style == "multipart_edits"
    assert shape.generate_url == "https://apihub.agnes-ai.com/v1/images/generations"
    assert shape.edits_url == "https://apihub.agnes-ai.com/v1/images/edits"


def test_declared_endpoint_wins_over_root_derivation():
    cfg = make_model_config(
        model_id="test-relay-images",
        provider="openrouter",
        name="Relay Images Test",
        image_output=True,
        protocol="openai_images",
        endpoint="https://relay.example.com/v1/images/generations",
    )
    shape = resolve_images_endpoint_shape(cfg)
    assert shape.generate_url == "https://relay.example.com/v1/images/generations"
    assert shape.edit_inline is True


def test_no_override_falls_back_to_official_derivation():
    # XXTF 式：未声明完整图像端点 -> 官方推导 {endpoint}/images/{generations,edits}
    cfg = make_model_config(
        model_id="test-std-images",
        provider="xxtf",
        name="Std Images Test",
        image_output=True,
        image_input=True,
        protocol="openai_images",
    )
    shape = resolve_images_endpoint_shape(cfg)
    assert shape.edit_inline is False
    assert shape.generate_url == "https://xxtf.baby/v1/images/generations"
    assert shape.edits_url == "https://xxtf.baby/v1/images/edits"


# ---------------------------------------------------------------------------
# 2. 公共路由（文本 / 视频 / 生图）
# ---------------------------------------------------------------------------

def test_resolve_model_route_by_capability_fields():
    assert resolve_model_route(SUPPORTED_MODELS["agnes-3.0-flash"]) == "chat"
    assert resolve_model_route(SUPPORTED_MODELS["agnes-image-2.5-flash"]) == "image"
    assert resolve_model_route(SUPPORTED_MODELS["agnes-video-2.5"]) == "video"
    # 防御：None 保守回落 chat
    assert resolve_model_route(None) == "chat"


def test_resolve_image_adapter_for_agnes_image_model():
    adapter = resolve_image_adapter(SUPPORTED_MODELS["agnes-image-2.5-flash"])
    assert adapter is IMAGE_PROTOCOLS["openai_images"]


def test_resolve_image_adapter_endpoint_fallback_for_misconfigured_protocol():
    # 最小配置：只写端点不写 protocol（协议继承厂商默认 openai_chat），
    # 公共分发按端点特征自动回退到 openai_images 适配器。
    cfg = make_model_config(
        model_id="test-endpoint-only",
        provider="agnes",
        name="Endpoint Only Test",
        image_output=True,
        endpoint=AGNES_IMAGES_URL,
    )
    assert get_effective_endpoint(cfg).protocol == "openai_chat"
    adapter = resolve_image_adapter(cfg)
    assert adapter is IMAGE_PROTOCOLS["openai_images"]


def test_resolve_image_adapter_rejects_non_image_protocol_without_endpoint():
    # anthropic_messages / gemini_native 等协议不具备图像生成链路，且未声明
    # 图像端点 -> 明确报错而不是静默回落（与重构前行为一致）。
    cfg = make_model_config(
        model_id="test-no-image-route",
        provider="anthropic",
        name="No Image Route Test",
    )
    with pytest.raises(ValueError):
        resolve_image_adapter(cfg)

    # 反向对照：同为无图像链路协议，但声明了 images 端点 -> 按端点路由成功
    cfg_with_ep = make_model_config(
        model_id="test-anthropic-with-images-ep",
        provider="anthropic",
        name="Anthropic With Images Endpoint",
        image_output=True,
        endpoint=AGNES_IMAGES_URL,
    )
    assert resolve_image_adapter(cfg_with_ep) is IMAGE_PROTOCOLS["openai_images"]


# ---------------------------------------------------------------------------
# 3. inline（Agnes 式）payload 硬约束
# ---------------------------------------------------------------------------

def test_inline_payload_text_to_image_uses_return_base64():
    payload = build_inline_images_payload(
        model="agnes-image-2.5-flash",
        prompt="cat",
        size="2K",
        ratio="16:9",
    )
    assert payload["model"] == "agnes-image-2.5-flash"
    assert payload["prompt"] == "cat"
    assert payload["size"] == "2K"
    assert payload["ratio"] == "16:9"
    assert payload["return_base64"] is True
    # 文档硬约束：response_format 绝不放顶层
    assert "response_format" not in payload
    assert "tags" not in payload
    assert "extra_body" not in payload


def test_inline_payload_edit_inlines_images_in_extra_body():
    payload = build_inline_images_payload(
        model="agnes-image-2.5-flash",
        prompt="make it orange",
        size="1K",
        ratio="1:1",
        image_data_urls=[PNG_1X1_DATA_URL],
    )
    # 参考图内联 extra_body.image（图生图）；response_format 在 extra_body 内
    assert payload["extra_body"]["image"] == [PNG_1X1_DATA_URL]
    assert payload["extra_body"]["response_format"] == "b64_json"
    assert "response_format" not in payload
    assert "tags" not in payload


def test_inline_payload_multi_image_composition():
    payload = build_inline_images_payload(
        model="agnes-image-2.5-flash",
        prompt="combine the two characters",
        size="2K",
        ratio="21:9",
        image_data_urls=[PNG_1X1_DATA_URL, PNG_1X1_DATA_URL],
    )
    assert len(payload["extra_body"]["image"]) == 2
    assert payload["ratio"] == "21:9"


def test_normalize_inline_size_and_ratio():
    # 档位大小写不敏感
    assert _normalize_inline_size("2k") == "2K"
    assert _normalize_inline_size("4K") == "4K"
    # 历史精确尺寸原样放行（网关自动标准化）
    assert _normalize_inline_size("1024x768") == "1024x768"
    assert _normalize_inline_size("1920x1080") == "1920x1080"
    # 非法值不发送
    assert _normalize_inline_size("") is None
    assert _normalize_inline_size("abc") is None
    # 官方 8 种宽高比全部放行，其它不发送
    for ratio in ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"):
        assert _normalize_inline_ratio(ratio) == ratio
    assert _normalize_inline_ratio("5:4") is None
    assert _normalize_inline_ratio("") is None


# ---------------------------------------------------------------------------
# 4. 请求层端到端（mock HTTP）：inline 形状真的 POST 到声明端点
# ---------------------------------------------------------------------------

class _FakeResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, body: dict):
        self._body = body

    async def text(self):
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _FakeSession:
    """捕获 POST url/payload 的最小 aiohttp.ClientSession 替身。"""

    captured = []

    def __init__(self, *args, **kwargs):
        pass

    def post(self, url, headers=None, json=None, data=None, **kwargs):
        _FakeSession.captured.append({"url": url, "json": json, "data": data})
        return _FakeResponse({"created": 1, "data": [{"b64_json": base64.b64encode(PNG_1X1).decode("ascii")}]})

    async def get(self, url, **kwargs):  # pragma: no cover - 参考图下载路径不走
        raise AssertionError("fake session should not GET")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


@pytest.fixture()
def fake_http(monkeypatch):
    _FakeSession.captured = []
    monkeypatch.setattr("ai.media_generation.aiohttp.ClientSession", _FakeSession)
    monkeypatch.setattr(app_config, "AGNES_API_KEY", "test-key")
    return _FakeSession.captured


def test_openai_compat_inline_text_to_image_posts_to_declared_endpoint(fake_http):
    cfg = SUPPORTED_MODELS["agnes-image-2.5-flash"]
    parsed, endpoint, detail, status, _req = asyncio.run(_request_openai_compat_image(
        cfg,
        prompt="A luminous floating city",
        image_urls=[],
        model="agnes-image-2.5-flash",
        aspect_ratio="16:9",
        image_size="2K",
    ))
    assert parsed is not None, detail
    assert status == 200
    assert endpoint == "/images/generations"
    assert len(fake_http) == 1
    assert fake_http[0]["url"] == AGNES_IMAGES_URL
    body = fake_http[0]["json"]
    assert body["size"] == "2K"
    assert body["ratio"] == "16:9"
    assert body["return_base64"] is True
    assert "response_format" not in body
    assert "tags" not in body


def test_openai_compat_inline_edit_posts_same_endpoint_with_extra_body_image(fake_http):
    cfg = SUPPORTED_MODELS["agnes-image-2.5-flash"]
    parsed, endpoint, detail, status, _req = asyncio.run(_request_openai_compat_image(
        cfg,
        prompt="Make the object orange while preserving the original composition",
        image_urls=[PNG_1X1_DATA_URL],
        model="agnes-image-2.5-flash",
        aspect_ratio="1:1",
        image_size="1K",
    ))
    assert parsed is not None, detail
    assert status == 200
    # 编辑与生成共用同一声明端点（Agnes 形状核心）
    assert fake_http[0]["url"] == AGNES_IMAGES_URL
    body = fake_http[0]["json"]
    assert body["extra_body"]["image"] == [PNG_1X1_DATA_URL]
    assert body["extra_body"]["response_format"] == "b64_json"
    assert "response_format" not in body
    assert "tags" not in body
    assert "image" not in body  # 参考图绝不在顶层


def test_openai_compat_multipart_style_still_posts_to_edits_endpoint(fake_http):
    # 未声明完整图像端点（endpoint 为厂商 API 根，官方形状推导）：
    # 编辑必须走独立 multipart /images/edits
    cfg = make_model_config(
        model_id="test-official-edit",
        provider="agnes",
        name="Official Edit Test",
        image_output=True,
        image_input=True,
        protocol="openai_images",
    )
    parsed, endpoint, detail, status, _req = asyncio.run(_request_openai_compat_image(
        cfg,
        prompt="remove the pedestrian",
        image_urls=[PNG_1X1_DATA_URL],
        model="test-official-edit",
        aspect_ratio="1:1",
    ))
    assert parsed is not None, detail
    assert endpoint == "/images/edits"
    assert fake_http[0]["url"] == "https://apihub.agnes-ai.com/v1/images/edits"
    # multipart 形状：payload 走 form（data），参考图在 image[] 文件字段
    assert fake_http[0]["json"] is None
    assert fake_http[0]["data"] is not None


# ---------------------------------------------------------------------------
# 5. 视频提交端点配置化
# ---------------------------------------------------------------------------

def test_agnes_video_endpoint_declared_in_config():
    ep = get_effective_endpoint(SUPPORTED_MODELS["agnes-video-2.5"])
    assert ep.endpoint == "https://apihub.agnes-ai.com/v1/videos"


def test_request_agnes_video_posts_to_declared_endpoint(monkeypatch):
    captured = {}

    class _FailResponse:
        status = 500
        headers = {}

        async def text(self):
            return "forced-stop"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class _CaptureSession:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            return _FailResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr("ai.media_generation.aiohttp.ClientSession", _CaptureSession)
    monkeypatch.setattr(app_config, "AGNES_API_KEY", "test-key")

    video_url, error, meta = asyncio.run(_request_agnes_video("a cat running", 5, "agnes-video-2.5"))
    # 500 是预期打断点：提交 URL 已捕获即达成断言目的
    assert video_url is None and error is not None
    assert captured["url"] == "https://apihub.agnes-ai.com/v1/videos"
    # Agnes Video 2.5 文档 schema：mode 必填；时长字段是字符串 seconds
    # （发 duration 会被网关 400 "duration is not an allowed request field"）
    assert captured["json"]["model"] == "agnes-video-2.5"
    assert captured["json"]["mode"] == "text"
    assert captured["json"]["seconds"] == "5"
    assert "duration" not in captured["json"]


def test_request_agnes_video_reference_mode_payload(monkeypatch):
    # 带参考图/参考视频 -> reference 模式 + 占位符注入（文生视频不携带媒体字段）
    captured = {}

    class _FailResponse:
        status = 500
        headers = {}

        async def text(self):
            return "forced-stop"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class _CaptureSession:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            return _FailResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr("ai.media_generation.aiohttp.ClientSession", _CaptureSession)
    monkeypatch.setattr(app_config, "AGNES_API_KEY", "test-key")

    video_url, error, meta = asyncio.run(_request_agnes_video(
        "让角色跑起来", 5, "agnes-video-2.5",
        reference_images=("https://r2.example/a.png", "https://r2.example/b.png"),
        reference_videos=("https://r2.example/motion.mp4",),
    ))
    assert video_url is None and error is not None  # 500 打断点
    body = captured["json"]
    assert body["mode"] == "reference"
    assert body["images"] == ["https://r2.example/a.png", "https://r2.example/b.png"]
    assert body["videos"] == [{"url": "https://r2.example/motion.mp4"}]
    assert "<Picture 1>" in body["prompt"] and "<Video 1>" in body["prompt"]


def test_request_agnes_video_full_cycle_polls_with_model_name_and_metadata_url(monkeypatch):
    # 闭环：提交 200（video_id）-> 轮询带 model_name -> completed 后从
    # metadata.url 取视频地址（文档推荐查询方式 + 响应字段）
    captured = {"posts": [], "gets": []}

    class _Resp:
        def __init__(self, body):
            self.status = 200
            self._body = body
            self.headers = {"Content-Type": "application/json"}

        async def text(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            captured["posts"].append({"url": url, "json": json})
            return _Resp('{"video_id": "video_abc", "status": "queued"}')

        def get(self, url, headers=None, params=None, **kwargs):
            captured["gets"].append({"url": url, "params": params})
            return _Resp('{"status": "completed", "progress": 100, "seconds": "7", "size": "720P", "metadata": {"url": "https://cdn.example/out.mp4"}}')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr("ai.media_generation.aiohttp.ClientSession", _Session)
    monkeypatch.setattr(app_config, "AGNES_API_KEY", "test-key")

    video_url, error, meta = asyncio.run(_request_agnes_video(
        "雨后夜景", 7, "agnes-video-2.5",
    ))
    assert error is None
    assert video_url == "https://cdn.example/out.mp4"
    # 轮询 URL 从声明提交端点的 host 推导；查询参数必须带 model_name
    assert captured["gets"][0]["url"] == "https://apihub.agnes-ai.com/agnesapi"
    assert captured["gets"][0]["params"] == {
        "video_id": "video_abc",
        "model_name": "agnes-video-2.5",
    }
    # meta 携带 seconds/size（供 caption 展示）
    assert meta["seconds"] == "7" and meta["size"] == "720P"
    # 提交体：seconds 为字符串，无 duration 字段
    assert captured["posts"][0]["json"]["seconds"] == "7"
    assert "duration" not in captured["posts"][0]["json"]


def test_request_agnes_video_failed_task_extracts_error_message(monkeypatch):
    # 失败任务：error 为对象 {message: ...}，需提取 message 而非嵌入 dict
    class _Resp:
        def __init__(self, body, status=200):
            self.status = status
            self._body = body
            self.headers = {"Content-Type": "application/json"}

        async def text(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class _Session:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            return _Resp('{"video_id": "video_fail", "status": "queued"}')

        def get(self, url, headers=None, params=None, **kwargs):
            return _Resp('{"status": "failed", "error": {"message": "Invalid reference media"}}')

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr("ai.media_generation.aiohttp.ClientSession", _Session)
    monkeypatch.setattr(app_config, "AGNES_API_KEY", "test-key")

    video_url, error, meta = asyncio.run(_request_agnes_video(
        "以参考图风格生成", 5, "agnes-video-2.5",
        reference_images=("https://r2.example/a.png",),
    ))
    assert video_url is None and error is not None
    assert "Invalid reference media" in error
    assert "message" not in error  # 不是 dict 的 json 残片


# ---------------------------------------------------------------------------
# 6. 分发链路贯通（task -> adapter -> 请求层 URL）
# ---------------------------------------------------------------------------

def test_dispatch_image_task_reaches_declared_endpoint(fake_http, monkeypatch):
    # R2 上传在结果后处理阶段发生；这里 monkeypatch 掉以隔离网络
    import ai.media_generation as mg
    monkeypatch.setattr(mg, "_upload_generated_images_to_r2", _fake_upload)

    task = ImageTask.generate("A clean product photo", model="agnes-image-2.5-flash",
                              aspect_ratio="16:9", image_size="2K")
    result = asyncio.run(dispatch_image_task(task))
    assert result.images, "应从 b64_json 解析出图片字节"
    assert fake_http[0]["url"] == AGNES_IMAGES_URL
    assert fake_http[0]["json"]["size"] == "2K"


async def _fake_upload(image_bytes_list):
    return [f"https://r2.example/fake_{i}.png" for i in range(len(image_bytes_list))]
