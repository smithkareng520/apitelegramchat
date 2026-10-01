"""错误卡片显示回归测试：机器错误文本中的 `***` 脱敏掩码不得被 Markdown 转换器吃掉。

背景（2026-09-11 生产 [5332ea8f]）：Agnes 网关对 400 报错文本里的 R2
域名/路径/预签名参数做了 `***` 脱敏掩码（11 个）。错误卡片构建时详情逐行
过 Markdown→HTML 转换器，发送层兜底（_rich_message_html_payload 第 0 步）
还会对整条消息再过一遍——两遍都会把 `***` 配对成 <b><i> 粗斜体：掩码消失、
报错被随机粗斜体切碎，用户看到 `https://.com///`、`.BadRequestError`
这类乱码；奇数残留的最后一个掩码以字面 `***` 幸存。

修复（两层）：
1. ai/error_formatting.py：机器错误文本中的 `*` 先统一替换为全角
`＊`（_neutralize_markdown_triggers）再进转换器——构建层的唯一一道
转换不再吃掩码；
2. 发送层对系统构建卡片改走 `pre_rendered=True` 跳过整篇二次转换
（用户要求"只转一次"；见 test_pre_rendered_rich_payload.py）。
本文件用生产事故原文锁定第 1 层行为与卡片的转换幂等性（后者作为
仍会重过转换器路径的纵深防御）。
"""
import asyncio

import httpx
from openai import BadRequestError

from ai.error_formatting import (
    _format_api_error_notice,
    _format_error_detail_for_display,
    _format_image_safety_notice,
    _neutralize_markdown_triggers,
    get_error_notification_message,
)
from markdown_converter import convert_markdown_to_telegram_html


def _bad_request(message: str) -> BadRequestError:
    """构造携带指定错误文本的真实 BadRequestError（与线上抛出形状一致）。"""
    response = httpx.Response(
        400, request=httpx.Request("POST", "https://gw.test/v1/chat/completions")
    )
    return BadRequestError(message, response=response, body=None)


# 生产事故 [5332ea8f] 的原始错误文本（流式响应 body 未读，e.body 为空，
# 卡片详情实际来自 str(exception)）。内层 JSON 的引号在 Python 字面量里
# 以 \" 形式出现，url 的单引号以 \' 转义——与上游回包逐字一致。
# 掩码共 11 个：***.BadRequestError 前 1 个 + https://***.com/***/***/*** 3 个
# + 六个 X-Amz-* 参数值 6 个 + 末尾 Algorithm=*** 与第 1 个共享计数——
# 奇数个配对后最后一个以字面 *** 幸存，正是线上乱码形态。
INCIDENT_ERROR_TEXT = (
    "Error code: 400 - {'error': {'message': '***.BadRequestError: OpenAIException - "
    '{\\"object\\":\\"error\\",\\"message\\":\\"An exception occurred while loading IMAGE data '
    "at index 0: Error while loading data ImageData(url=\\'https://***.com/***/***/*** "
    "Timed out while downloading media URL: https://***.com/***/***/***?X-Amz-Credential=***"
    "&X-Amz-Date=***&X-Amz-Expires=***&X-Amz-SignedHeaders=***&X-Amz-Signature=***"
    "&X-Amz-Algorithm=***', 'type': 'upstream_error', 'param': '', 'code': '400'}}"
)


class TestNeutralizeMarkdownTriggers:
    def test_asterisk_runs_become_fullwidth(self):
        assert _neutralize_markdown_triggers("***.BadRequestError") == "＊＊＊.BadRequestError"
        assert _neutralize_markdown_triggers("a*b*c") == "a＊b＊c"

    def test_url_masks_preserved(self):
        text = "https://***.com/***/***?X-Amz-Credential=***&X-Amz-Date=***"
        assert _neutralize_markdown_triggers(text) == (
            "https://＊＊＊.com/＊＊＊/＊＊＊?X-Amz-Credential=＊＊＊&X-Amz-Date=＊＊＊"
        )

    def test_idempotent(self):
        once = _neutralize_markdown_triggers(INCIDENT_ERROR_TEXT)
        assert _neutralize_markdown_triggers(once) == once

    def test_no_asterisk_passthrough(self):
        assert _neutralize_markdown_triggers("普通错误文本") == "普通错误文本"

    def test_empty_safe(self):
        assert _neutralize_markdown_triggers("") == ""


class TestFormatErrorDetailDisplay:
    def test_fallback_path_neutralizes_masks(self):
        # 非 JSON 文本走 fallback 分支：掩码必须原样保留为全角星号
        result = _format_error_detail_for_display("***x*** y")
        assert result == "＊＊＊x＊＊＊ y"
        assert "<b>" not in result and "<i>" not in result

    def test_payload_path_neutralizes_masks(self):
        # Python 字面量可解析的 payload 走结构化提取分支，同样要惰性化
        detail = "{'error': {'message': 'gateway masked *** key', 'code': '429'}}"
        result = _format_error_detail_for_display(detail)
        assert "gateway masked ＊＊＊ key" in result
        assert "<b><i>" not in result

    def test_incident_masks_survive_conversion(self):
        result = _format_error_detail_for_display(INCIDENT_ERROR_TEXT)
        assert "＊＊＊.BadRequestError" in result
        assert "https://＊＊＊.com" in result
        assert "<b><i>" not in result and "</i></b>" not in result


class TestApiErrorNoticeCard:
    def test_incident_card_has_no_bold_italic_fragments(self):
        card = _format_api_error_notice(
            api_name="Agnes 3.0 Flash",
            error_code=400,
            model="agnes-3.0-flash",
            detail=_format_error_detail_for_display(INCIDENT_ERROR_TEXT),
        )
        # 卡片头部与结构字段正常
        assert card.startswith("⚠️ <b>Agnes 3.0 Flash 请求失败</b>")
        assert "HTTP 状态：400" in card
        assert "模型：agnes-3.0-flash" in card
        # 掩码以全角星号原样可见，粗斜体碎片绝迹
        assert "＊＊＊.BadRequestError" in card
        assert "<b><i>" not in card and "</i></b>" not in card
        # 任何半角星号都不应残留（全部已惰性化）
        assert "*" not in card

    def test_incident_card_stable_under_any_reconversion(self):
        # 现版发送层对错误卡片走 pre_rendered=True 跳过二次转换；但任何
        # 仍会重过转换器的路径（旧版调用点/未来回归）都必须对该卡片
        # 幂等：原样返回。保留此断言作为纵深防御。
        card = _format_api_error_notice(
            api_name="Agnes 3.0 Flash",
            error_code=400,
            model="agnes-3.0-flash",
            detail=_format_error_detail_for_display(INCIDENT_ERROR_TEXT),
        )
        assert convert_markdown_to_telegram_html(card) == card


class TestGetErrorNotificationMessage:
    def test_full_production_path(self):
        # 生产调用形状（ai_handlers.get_ai_response 顶层异常）：流式响应
        # body 未读 → error_message 是脱敏占位，真实文本只在 exception 里。
        exc = _bad_request(INCIDENT_ERROR_TEXT)
        card = asyncio.run(
            get_error_notification_message(
                7162243624,
                error_code=400,
                error_message="内部错误 (error_id=5b4f93a8b4f1)",
                api_name="Agnes 3.0 Flash",
                exception=exc,
                endpoint="/v1/chat/completions",
                model="agnes-3.0-flash",
            )
        )
        # 结构化提取生效（来自 payload 的 代码/消息/类型 标签）
        assert "代码：400" in card
        assert "类型：upstream_error" in card
        # 掩码可见、无粗斜体碎片、无残留半角星号
        assert "＊＊＊.BadRequestError" in card
        assert "https://＊＊＊.com/＊＊＊/＊＊＊/＊＊＊?X-Amz-Credential=＊＊＊" in card
        assert "<b><i>" not in card and "</i></b>" not in card
        assert "*" not in card
        # 脱敏占位的 error_message 不应出现在卡片里（真正详情来自 exception）
        assert "内部错误" not in card
        # 卡片对转换器幂等（纵深防御；现版发送层已改走 pre_rendered 跳过二次转换）
        assert convert_markdown_to_telegram_html(card) == card


class TestImageSafetyNotice:
    def test_masks_neutralized_in_safety_notice(self):
        notice = _format_image_safety_notice(
            detail="ImageData(url='https://***.com/***/***') rejected by filter",
            model="Z-Image-Turbo",
        )
        assert "https://＊＊＊.com/＊＊＊/＊＊＊" in notice
        assert "<i>详情：" in notice
        assert "<b><i>" not in notice
        assert "*" not in notice
