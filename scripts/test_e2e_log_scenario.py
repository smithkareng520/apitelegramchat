"""端到端验证：模拟日志中的完整场景，确认兜底清理能避免整条消息失败。"""
import sys
sys.path.insert(0, "/home/z/my-project/work/src")

from apitelegramchat.utils import _rich_message_html_payload

# 日志中实际的 HTML（带伪 URL <img src="photo_AgACAgUA.jpg"/>）
log_html = (
    '<details><summary>用户问我图片中的人穿了什么衣服。'
    '让我看一下这张图片。\n从图…</summary>'
    '<p>用户问我图片中的人穿了什么衣服。让我看一下这张图片。\n'
    '从图片来看，这位女性穿着一件白色的紧身连体衣，'
    '看起来像是泳装或者舞蹈练功服，有细肩带设计，领口是圆领，高腰设计。'
    '这应该是一件白色的连体泳衣或紧身连体衣。</p>'
    '</details>'
    '<figure><img src="photo_AgACAgUA.jpg"/></figure>'
    '她穿了一件白色的紧身连体衣，设计简约：'
    '<ul>'
    '<li>细肩带款式</li>'
    '<li>圆领口设计</li>'
    '<li>高腰剪裁</li>'
    '<li>紧身包身版型</li>'
    '</ul>'
    '这种款式通常用于游泳、舞蹈练习或作为基础打底服装。'
)

# 模拟 send_rich_html_message 中构造 payload 的过程
payload = _rich_message_html_payload(log_html)
cleaned_html = payload["html"]

print("===== 原始 HTML 长度 =====", len(log_html))
print("===== 清理后 HTML 长度 =====", len(cleaned_html))
print()
print("===== 清理后 HTML =====")
print(cleaned_html)
print()

# 关键验证点
assert "photo_AgACAgUA.jpg" not in cleaned_html, "伪 URL 必须被剥离"
assert "<figure>" not in cleaned_html, "空 figure 必须被清理"
assert "细肩带款式" in cleaned_html, "正文 ul/li 必须保留"
assert "<details>" in cleaned_html, "details 块必须保留"
assert "<p>" in cleaned_html, "p 段落必须保留"
assert "<ul>" in cleaned_html, "ul 列表必须保留"
print("[PASS] 兜底清理成功剥离伪 URL，保留所有正文内容")
print("[PASS] 此 HTML 提交给 Telegram sendRichMessage 将不再触发 RICH_MESSAGE_PHOTO_URL_INVALID")
