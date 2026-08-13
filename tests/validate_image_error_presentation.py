import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from apitelegramchat.tool_executors import format_tool_result  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def test_rate_limit_error_is_a_friendly_quote_card() -> None:
    raw = """❌ ModelScope 图像接口 请求失败
HTTP 状态：429
模型：Tongyi-MAI/Z-Image-Turbo
Request ID：e12bf114-078b-4fd8-a7ec-99c5f640229f
详情：Request ID：e12bf114-078b-4fd8-a7ec-99c5f640229f"""

    summary, details_html = await format_tool_result(
        "generate_image_from_text",
        {"prompt": "一只小猫"},
        raw,
    )

    require(summary == "🎨 图片生成暂不可用", "图片限流必须使用失败摘要而不是成功摘要")
    require("<p><b>图片暂时无法生成</b></p>" in details_html, "卡片必须有清晰的用户提示标题")
    require("当前图片服务请求较多，请稍后再试；无需修改你的描述。" in details_html, "429 必须给出准确、可操作的提示")
    require("<blockquote><b>诊断信息</b><br/>" in details_html, "调试信息必须使用 Telegram 支持的引用块")
    require("服务繁忙（HTTP 429）" in details_html, "诊断信息必须保留状态语义")
    require("Tongyi-MAI/Z-Image-Turbo" in details_html, "诊断信息必须保留模型名称")
    require(details_html.count("e12bf114-078b-4fd8-a7ec-99c5f640229f") == 1, "重复 Request ID 必须折叠为一处")
    require("详情：" not in details_html, "原始不友好的详情标签不应直接暴露")


async def test_server_error_has_a_safe_action() -> None:
    raw = """❌ ModelScope 图像接口 请求失败
HTTP 状态：503
模型：Tongyi-MAI/Z-Image-Turbo
详情：upstream overloaded"""
    _, details_html = await format_tool_result("generate_image_from_text", {}, raw)
    require("图片服务暂时不可用，请稍后再试。" in details_html, "5xx 必须提示稍后重试")
    require("服务异常（HTTP 503）" in details_html, "5xx 诊断应显示语义化状态")
    require("服务响应：</b>upstream overloaded" in details_html, "非重复详情应保留在引用诊断内")


async def main() -> None:
    await test_rate_limit_error_is_a_friendly_quote_card()
    await test_server_error_has_a_safe_action()
    print("image error presentation validation: PASS")


if __name__ == "__main__":
    asyncio.run(main())
