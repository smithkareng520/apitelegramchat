import re
from apitelegramchat.utils import _escape_media_src_urls, escape_html_href_url


def test_media_src_and_download_href_have_different_encoding_rules():
    url = (
        "https://cdn.example.com/generated/file.mp4?"
        "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
        "X-Amz-Credential=a%2Fb&X-Amz-Signature=abc123"
    )
    html = (
        f'<figure><video src="{url}"></video>'
        f'<figcaption><a href="{escape_html_href_url(url)}">下载 / 查看视频</a>'
        "</figcaption></figure>"
    )

    out = _escape_media_src_urls(html)

    assert f'src="{url.replace("&", "&amp;")}"' in out
    href = re.search(r'href="([^"]+)"', out).group(1)
    assert href == url
    assert "&amp;" not in href


def test_href_helper_preserves_ampersands():
    url = "https://example.com/download?foo=1&bar=2"
    assert escape_html_href_url(url) == url


if __name__ == "__main__":
    test_media_src_and_download_href_have_different_encoding_rules()
    test_href_helper_preserves_ampersands()
    print("PASS: URL attribute regression tests")
