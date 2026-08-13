import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import apitelegramchat.utils as utils  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeResponse:
    def __init__(self, status: int, *, body: str = "", message_id: int | None = None):
        self.status = status
        self._body = body
        self._message_id = message_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self) -> str:
        return self._body

    async def json(self) -> dict:
        if self._message_id is None:
            return {}
        return {"result": {"message_id": self._message_id}}


class FakeSession:
    responses: list[FakeResponse] = []
    sent_payloads: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, json: dict):
        self.sent_payloads.append(json)
        if not self.responses:
            raise AssertionError("测试未准备足够的模拟响应")
        return self.responses.pop(0)


def test_video_html_becomes_explicit_media_reference() -> None:
    source_url = "https://cdn.example.test/generated/cat.mp4?X-Amz-Signature=a&X-Amz-Date=b"
    rich = utils._build_rich_message_from_html(
        f'<figure><video src="{source_url}"></video><figcaption>小猫</figcaption></figure>'
    )
    require("content" not in rich, "InputRichMessage 只能使用 html、markdown 或 blocks 之一")
    require(len(rich.get("media", [])) == 1, "HTTP(S) 视频必须产生一条显式媒体记录")
    media = rich["media"][0]
    require(media["id"].startswith("video_"), "媒体 id 必须使用合法的视频前缀")
    require(media["media"]["type"] == "video", "媒体类型必须为 video")
    require(media["media"]["media"] == source_url, "媒体 URL 不得保留 HTML 实体转义")
    require(
        f'tg://video?id={media["id"]}' in rich["html"],
        "HTML 视频 src 必须引用 media 数组中的 tg://video id",
    )


def test_draft_downgrades_video_to_link() -> None:
    source_url = "https://cdn.example.test/generated/cat.mp4"
    fallback = utils._rich_message_video_link_fallback(
        f'<figure><video src="{source_url}"></video><figcaption>小猫</figcaption></figure>'
    )
    require("<video" not in fallback.lower(), "草稿回退不得保留视频媒体块")
    require(source_url in fallback, "草稿回退必须保留可访问的视频链接")
    require("查看视频" in fallback, "草稿回退必须具有用户可见的链接文本")


async def test_send_uses_media_array() -> None:
    original_session = utils.aiohttp.ClientSession
    FakeSession.responses = [FakeResponse(200, message_id=6789)]
    FakeSession.sent_payloads = []
    utils.aiohttp.ClientSession = FakeSession
    try:
        result = await utils.send_rich_html_message(
            1234,
            '<figure><video src="https://cdn.example.test/cat.mp4"></video></figure>',
            reassert_draft=False,
        )
        require(result == 6789, "成功发送时必须返回 Telegram message_id")
        rich = FakeSession.sent_payloads[0]["rich_message"]
        require("html" in rich and "media" in rich, "永久消息必须同时发送 html 和媒体数组")
        require("content" not in rich, "永久消息不得发送未定义的 content 字段")
    finally:
        utils.aiohttp.ClientSession = original_session


async def test_video_fetch_error_retries_with_link() -> None:
    original_session = utils.aiohttp.ClientSession
    FakeSession.responses = [
        FakeResponse(
            400,
            body='{"ok":false,"description":"Bad Request: RICH_MESSAGE_VIDEO_NO_MEDIA_FOUND"}',
        ),
        FakeResponse(200, message_id=6790),
    ]
    FakeSession.sent_payloads = []
    utils.aiohttp.ClientSession = FakeSession
    try:
        result = await utils.send_rich_html_message(
            1234,
            '<figure><video src="https://cdn.example.test/cat.mp4"></video></figure>',
            reassert_draft=False,
        )
        require(result == 6790, "媒体抓取失败后，安全链接回退必须可发送")
        require(len(FakeSession.sent_payloads) == 2, "视频媒体错误时必须仅重试一次")
        fallback_rich = FakeSession.sent_payloads[1]["rich_message"]
        require("media" not in fallback_rich, "回退消息不得继续触发视频媒体抓取")
        require("<video" not in fallback_rich["html"].lower(), "回退消息不得保留 video 标签")
        require("查看视频" in fallback_rich["html"], "回退消息必须给出视频链接")
    finally:
        utils.aiohttp.ClientSession = original_session


def main() -> None:
    test_video_html_becomes_explicit_media_reference()
    test_draft_downgrades_video_to_link()
    asyncio.run(test_send_uses_media_array())
    asyncio.run(test_video_fetch_error_retries_with_link())
    print("rich message video media validation: PASS")


if __name__ == "__main__":
    main()
