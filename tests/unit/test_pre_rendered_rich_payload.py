"""pre_rendered 旗标回归测试：系统构建的最终 HTML 不再被发送层二次转换。

背景（2026-09-11 [5332ea8f] 错误卡片乱码事故 + 用户要求"只转一次"）：
错误卡片构建层（ai/error_formatting.py）逐字段/逐行跑过 Markdown→HTML
转换器后，发送层兜底（_rich_message_html_payload 第 0 步）又对整条消息
再跑一遍——机器错误文本里的 ``***`` 脱敏掩码、URL 参数等形状会被第二遍
误判为 Markdown 标记（粗斜体定界符配对），正是乱码事故的根源；多跑一遍
转义也是纯风险没有收益。

修复：_rich_message_html_payload / send_rich_html_message 增加
``pre_rendered`` 旗标——调用方声明内容已是最终 HTML 时，发送层跳过
第 0 步整篇 Markdown 转换，只保留两道结构性安全网（<tg-button> 强模式
校验、非法媒体 URL 剥离/观看页降级），两者对任意输入幂等。
"""
import inspect

from core.rich_media import _rich_message_html_payload
from core.telegram_messaging import send_rich_html_message


# 含典型 Markdown 语法的模型内容（默认路径必须转换）
MD_TEXT = "说明 **加粗** 与 ![图](https://x/y.png) 混排"


class TestPreRenderedSkipConversion:
    def test_default_path_still_converts_markdown(self):
        # 向后兼容：不传旗标时行为与旧版一致（模型内容路径）
        payload = _rich_message_html_payload(MD_TEXT)
        assert "<b>加粗</b>" in payload["html"]
        assert '<img src="https://x/y.png"/>' in payload["html"]
        assert payload["skip_entity_detection"] is True

    def test_pre_rendered_keeps_text_byte_identical(self):
        payload = _rich_message_html_payload(MD_TEXT, pre_rendered=True)
        assert payload["html"] == MD_TEXT

    def test_pre_rendered_preserves_incident_masks(self):
        # 事故原文形状：半角 *** 掩码在 pre_rendered 下必须逐字保留，
        # 不再被第二遍转换配对成 <b><i>
        raw = "详情：消息：***.BadRequestError ... https://***.com/***/***/***"
        payload = _rich_message_html_payload(raw, pre_rendered=True)
        assert payload["html"] == raw
        assert "<b><i>" not in payload["html"]

    def test_default_path_would_mangle_masks_proves_need(self):
        # 对照组：同一段文本走默认路径（两次转换形态）会被误转换——
        # 锁定"pre_rendered 是必要的"这一事实，防止未来被当作冗余删除
        raw = "a ***b*** c"
        converted = _rich_message_html_payload(raw)["html"]
        assert "<b><i>b</i></b>" in converted


class TestPreRenderedKeepsStructuralSafetyNets:
    def test_invalid_media_still_stripped(self):
        html = '<p>文字说明</p><img src="photo_AgACAgUA.jpg"/>'
        payload = _rich_message_html_payload(html, pre_rendered=True)
        assert "img" not in payload["html"]
        assert "文字说明" in payload["html"]

    def test_valid_media_kept_byte_identical(self):
        html = (
            '<figure><img src="https://r2.example/x.png?X-Amz-Signature=a&amp;b"/>'
            "<figcaption>1024x1024 PNG</figcaption></figure>"
        )
        payload = _rich_message_html_payload(html, pre_rendered=True)
        assert payload["html"] == html

    def test_watch_page_video_still_demoted(self):
        html = (
            '<figure><video src="https://www.youtube.com/watch?v=abc"></video>'
            "<figcaption>视频标题</figcaption></figure>"
        )
        payload = _rich_message_html_payload(html, pre_rendered=True)
        assert "<video" not in payload["html"]
        assert "youtube.com/watch?v=abc" in payload["html"]
        assert "视频标题" in payload["html"]

    def test_invalid_button_still_escaped(self):
        html = "<p>你好</p><tg-button>缺 type 的按钮</tg-button>"
        payload = _rich_message_html_payload(html, pre_rendered=True)
        assert "<tg-button>" not in payload["html"]
        assert "&lt;tg-button&gt;" in payload["html"]


class TestSendRichHtmlMessagePassthrough:
    def test_signature_has_pre_rendered(self):
        # 发送入口必须把旗标透传给 payload 构造（payload 层行为已由上面
        # 用例锁定；这里锁住 API 形状防止签名回退）
        sig = inspect.signature(send_rich_html_message)
        param = sig.parameters.get("pre_rendered")
        assert param is not None
        assert param.default is False

    def test_flag_is_keyword_only_safe(self):
        # pre_rendered 是带默认值的可选参数：既有调用方（大量 send_rich_html_message
        # 调用点）零改动兼容
        sig = inspect.signature(send_rich_html_message)
        names = list(sig.parameters)
        assert names.index("pre_rendered") > names.index("chat_id")
