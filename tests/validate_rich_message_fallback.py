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


def test_plain_text_fallback_normalizes_visible_content() -> None:
    fallback = utils._rich_message_plain_text_fallback(
        "<details><summary>执行记录</summary><i>已完成 &amp; 已保存</i></details>"
    )
    require(
        fallback == "<p>执行记录已完成 &amp; 已保存</p>",
        "回退消息必须提取可见文本、转义并包装为安全段落",
    )
    require(
        utils._rich_message_plain_text_fallback("<details><summary></summary></details>") == "",
        "没有可见文字时不得构造伪内容回退",
    )


async def test_permanent_send_retries_once_with_safe_paragraph() -> None:
    original_session = utils.aiohttp.ClientSession
    FakeSession.sent_payloads = []
    FakeSession.responses = [
        FakeResponse(
            400,
            body='{"ok":false,"description":"Bad Request: RICH_MESSAGE_CONTENT_REQUIRED"}',
        ),
        FakeResponse(200, message_id=2468),
    ]
    utils.aiohttp.ClientSession = FakeSession
    try:
        result = await utils.send_rich_html_message(
            1357,
            "<details><summary>步骤</summary><i>完成</i></details>",
            reassert_draft=False,
        )
        require(result == 2468, "安全段落回退成功时必须返回新消息 ID")
        require(len(FakeSession.sent_payloads) == 2, "内容错误时必须只额外尝试一次回退发送")
        first_html = FakeSession.sent_payloads[0]["rich_message"]["html"]
        second_html = FakeSession.sent_payloads[1]["rich_message"]["html"]
        require(first_html == "<details><summary>步骤</summary><i>完成</i></details>", "首发必须保留原 HTML")
        require(second_html == "<p>步骤完成</p>", "回退必须为服务端可接受的安全段落")
        require("content" not in FakeSession.sent_payloads[0]["rich_message"], "InputRichMessage 不得包含未定义 content 字段")
    finally:
        utils.aiohttp.ClientSession = original_session


def main() -> None:
    test_plain_text_fallback_normalizes_visible_content()
    asyncio.run(test_permanent_send_retries_once_with_safe_paragraph())
    print("rich message fallback validation: PASS")


if __name__ == "__main__":
    main()
