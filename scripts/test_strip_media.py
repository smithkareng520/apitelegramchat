"""验证 _strip_invalid_media_urls 在日志中实际场景下的清理行为。"""
import sys
sys.path.insert(0, "/home/z/my-project/work/src")
from apitelegramchat.utils import _strip_invalid_media_urls


def test_log_scenario():
    """日志中的实际 HTML：含伪 URL <img src='photo_AgACAgUA.jpg'/>。"""
    html = (
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
    cleaned = _strip_invalid_media_urls(html)
    print("===== 原始 HTML =====")
    print(html)
    print()
    print("===== 清理后 HTML =====")
    print(cleaned)
    print()
    assert 'photo_AgACAgUA.jpg' not in cleaned, "伪 URL 应被剥离"
    assert '<figure>' not in cleaned, "空 figure 应被清理"
    assert '细肩带款式' in cleaned, "正文文字应保留"
    print("[PASS] 伪 URL 已剥离，正文文字保留")


def test_keep_valid_url():
    """合法 http(s) URL 的图片应被保留。"""
    html = '<p>看这张图：</p><figure><img src="https://example.com/foo.png"/><figcaption>测试</figcaption></figure>'
    cleaned = _strip_invalid_media_urls(html)
    print("===== 合法 URL 场景 =====")
    print(cleaned)
    assert 'https://example.com/foo.png' in cleaned
    assert '<figure>' in cleaned
    print("[PASS] 合法 URL 图片被保留")


def test_strip_invalid_video():
    """video 标签 + 非法 src 时整块删除。"""
    html = '<p>开始</p><video src="video_xyz.mp4">fallback</video><p>结束</p>'
    cleaned = _strip_invalid_media_urls(html)
    print("===== 非法 video src 场景 =====")
    print(cleaned)
    assert 'video_xyz.mp4' not in cleaned
    assert 'fallback' not in cleaned, "video 内部内容应一起删除"
    assert '<p>开始</p>' in cleaned
    assert '<p>结束</p>' in cleaned
    print("[PASS] 非法 video 整块删除，正文保留")


def test_only_invalid_media():
    """只有伪 URL 的图，清理后应为空。"""
    html = '<figure><img src="photo_abc.jpg"/></figure>'
    cleaned = _strip_invalid_media_urls(html)
    print("===== 只有伪 URL 场景 =====")
    print(repr(cleaned))
    assert cleaned.strip() == ""
    print("[PASS] 全是伪 URL 时返回空字符串")


if __name__ == "__main__":
    test_log_scenario()
    print()
    test_keep_valid_url()
    print()
    test_strip_invalid_video()
    print()
    test_only_invalid_media()
    print()
    print("=== 所有测试通过 ===")
