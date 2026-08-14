"""富媒体兼容模块。

URL 与媒体标签不在此模块中进行清洗、HTML 实体转义、解码、改写或格式规范化。
调用方传入的内容将原样返回。
"""


def normalize_rich_media_html(html_content: str) -> str:
    """兼容旧调用方；不对内容或 URL 做任何处理。"""
    return html_content
