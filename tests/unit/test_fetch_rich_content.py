# =====================================================================
# tests/unit/test_fetch_rich_content.py — fetch_url 富内容提取引擎
# =====================================================================
# 被测关键路径：fetch_rich_content.py 的媒体收集/锚定/穿插/截断/兜底。
# 重点回归：
#   - 图片不限量（按原始结构全量收集，仅受 token 预算约束）
#   - 图片必须出现在原始结构位置（宽松 URL 锚定 + 零锚点比例兜底）
#   - 单块超预算时头尾截断而不是整页丢弃
#   - 隐藏元素判定按 class 分词，不误杀 hidden-print
#   - 兜底文本剔除 nav/footer/cookie 弹窗等噪音段落
# =====================================================================
import pytest

from fetch_rich_content import (
    DomMedia,
    _anchor_entries,
    _assign_proportional_anchor_orders,
    _collect_dom_media,
    _interleave,
    _is_hidden_element,
    _is_probably_decorative,
    _is_punct_only,
    _media_url_key,
    _parse_dom,
    _pick_srcset_best,
    _sanitize_url,
    _truncate_blocks,
    build_fallback_text_from_html,
    build_model_facing_html,
    extract_title_from_html,
)
from token_budget import count_tokens


BASE = "https://example.com/article"


# ---------------------------------------------------------------------
# URL 清洗 / 图片选择
# ---------------------------------------------------------------------
def test_sanitize_url_blocks_dangerous_schemes():
    assert _sanitize_url("javascript:alert(1)") is None
    assert _sanitize_url("data:text/html;base64,AAAA") is None
    assert _sanitize_url("file:///etc/passwd") is None
    assert _sanitize_url("#anchor") is None


def test_sanitize_url_resolves_relative_and_drops_fragment():
    out = _sanitize_url("/img/a.jpg#frag", BASE)
    assert out == "https://example.com/img/a.jpg"


def test_pick_srcset_best_prefers_largest_width():
    srcset = "a.jpg 320w, b.jpg 1280w, c.jpg 640w"
    assert _pick_srcset_best(srcset) == "b.jpg"


def test_is_probably_decorative_filters_icons_and_tracking_pixels():
    assert _is_probably_decorative("https://x.com/img/icon.png")
    assert _is_probably_decorative("https://x.com/img/sprite.svg")
    assert _is_probably_decorative("https://x.com/tracking/1x1.gif")
    assert not _is_probably_decorative("https://x.com/img/photo-2024.jpg")


def test_media_url_key_drops_query_and_is_case_insensitive_host():
    key1 = _media_url_key("https://CDN.Example.com/a/img.jpg?w=880")
    key2 = _media_url_key("https://cdn.example.com/a/img.jpg?x-oss-process=large")
    assert key1 == key2 == ("cdn.example.com", "/a/img.jpg")


# ---------------------------------------------------------------------
# 隐藏元素 / 碎片段落
# ---------------------------------------------------------------------
def _el(attrs):
    tree = _parse_dom("<html><body><img " + " ".join(
        f'{k}="{v}"' for k, v in attrs.items()
    ) + "/></body></html>")
    return tree.find(".//img")


def test_is_hidden_element_class_tokens_no_substring_false_positive():
    assert _is_hidden_element(_el({"src": "a.jpg", "class": "hidden"}))
    assert _is_hidden_element(_el({"src": "a.jpg", "class": "hidden-xs"}))
    # hidden-print 仅打印时隐藏，屏幕上可见 → 不应过滤
    assert not _is_hidden_element(_el({"src": "a.jpg", "class": "hidden-print"}))
    assert not _is_hidden_element(_el({"src": "a.jpg", "class": "not-hidden"}))
    # 零尺寸兼容 0px / 0% 写法
    assert _is_hidden_element(_el({"src": "a.jpg", "width": "0"}))
    assert _is_hidden_element(_el({"src": "a.jpg", "height": "0px"}))


def test_is_punct_only_detects_fragment_paragraphs():
    assert _is_punct_only("：")
    assert _is_punct_only("、。·—")
    assert _is_punct_only("")
    assert not _is_punct_only("这是正常中文段落")


# ---------------------------------------------------------------------
# 图片不限量：20 张正文图片全部收集
# ---------------------------------------------------------------------
def test_collect_dom_media_images_are_not_capped():
    imgs = "".join(
        f'<img src="https://example.com/gallery/pic-{i:02d}.jpg"/>' for i in range(20)
    )
    tree = _parse_dom(f"<html><body><p>正文</p>{imgs}</body></html>")
    media = _collect_dom_media(tree, BASE)
    images = [m for m in media if m.kind == "image"]
    assert len(images) == 20


def test_collect_dom_media_still_filters_hidden_and_boilerplate():
    html = (
        "<html><body>"
        '<nav><img src="https://example.com/nav/banner-art.jpg"/></nav>'
        '<img src="https://example.com/pic/visible.jpg"/>'
        '<img src="https://example.com/pic/gone.jpg" style="display:none"/>'
        "</body></html>"
    )
    tree = _parse_dom(html)
    media = _collect_dom_media(tree, BASE)
    urls = {m.url for m in media}
    assert "https://example.com/pic/visible.jpg" in urls
    assert "https://example.com/pic/gone.jpg" not in urls
    # 样板区图片仍会收集（带 boilerplate 标记，插入阶段跳过）
    nav = [m for m in media if m.url == "https://example.com/nav/banner-art.jpg"]
    assert nav and nav[0].boilerplate


# ---------------------------------------------------------------------
# 锚定：宽松 URL 匹配 + 零锚点比例兜底
# ---------------------------------------------------------------------
def test_anchor_entries_relaxed_url_match_ignores_query_diff():
    html = (
        '<html><body><p>第一段足够长的正文文本内容。</p>'
        '<img src="https://cdn.example.com/a/img.jpg?w=200"/>'
        "<p>第二段足够长的正文文本内容。</p></body></html>"
    )
    tree = _parse_dom(html)
    media = _collect_dom_media(tree, "https://cdn.example.com/a/post")
    # trafilatura 产出的块里 src 不带 query（重选尺寸后参数变化）
    entries = [{"html": '<img src="https://cdn.example.com/a/img.jpg"/>',
                "order": None, "path": None}]
    _anchor_entries(entries, tree, media)
    assert entries[0]["order"] is not None


def test_assign_proportional_anchor_orders_preserves_block_order():
    media = [DomMedia(order_idx=o, path=f"/x[{o}]", kind="image",
                      url=f"https://e.com/{o}.jpg") for o in (10, 500, 900)]
    entries = [{"html": f"<p>第{i}段足够长的正文内容文本。</p>", "order": None, "path": None}
               for i in range(6)]
    _assign_proportional_anchor_orders(entries, media)
    orders = [e["order"] for e in entries]
    assert orders == sorted(orders)          # 保序：块顺序不变
    assert all(o is not None for o in orders)
    assert max(orders) <= 900


def test_interleave_spreads_media_instead_of_dumping_at_tail():
    # 模拟零锚点场景：3 个正文块 + 2 张 DOM 图片，比例兜底后应穿插
    media = [
        DomMedia(order_idx=100, path="/p[1]/img[1]", kind="image", url="https://e.com/1.jpg"),
        DomMedia(order_idx=300, path="/p[2]/img[1]", kind="image", url="https://e.com/2.jpg"),
    ]
    entries = [{"html": f"<p>第{i}段足够长的正文内容。</p>", "order": None, "path": None}
               for i in range(3)]
    _assign_proportional_anchor_orders(entries, media)
    result = _interleave(entries, [(m.order_idx, f'<img src="{m.url}"/>') for m in media])
    joined = "\n".join(result)
    # 核心诉求：至少一张图穿插在最后一个文本块之前（而非全部堆到尾部），
    # 且图片保持文档相对顺序；最后一张图贴近文档末尾是真实结构的反映。
    assert joined.index("1.jpg") < joined.rindex("<p>")
    assert joined.index("1.jpg") < joined.index("2.jpg")
    assert len(result) == 5


# ---------------------------------------------------------------------
# 截断：整块优先，单块超预算头尾兜底
# ---------------------------------------------------------------------
def test_truncate_blocks_keeps_complete_blocks_within_budget():
    blocks = ["<p>" + "内容段落。" * 10 + "</p>", "<p>短段。</p>"]
    kept, truncated = _truncate_blocks(blocks, count_tokens(blocks[0]) + 50)
    assert kept == blocks and not truncated


def test_truncate_blocks_squeezes_oversized_first_block_instead_of_dropping():
    huge = "<table>" + "".join(
        f"<tr><td>行{i}：这一行有很长的描述文本用来撑大体积。</td></tr>" for i in range(4000)
    ) + "</table>"
    small = "<p>结尾段落。</p>"
    kept, truncated = _truncate_blocks([huge, small], 900)
    assert truncated
    assert len(kept) == 1
    assert kept[0].startswith("<p>") and "…[此块超长，已头尾截断]" in kept[0]
    assert count_tokens(kept[0]) <= 900
    # 头尾都在：保留了表格开头与结尾的信息
    assert "行0" in kept[0] and "行3999" in kept[0]


def test_truncate_blocks_drops_middle_oversized_block_with_notice():
    b1 = "<p>" + "开头段落内容。" * 20 + "</p>"
    huge = "<p>" + "中间超长段落。" * 3000 + "</p>"
    b3 = "<p>结尾。</p>"
    budget = count_tokens(b1) + 50
    kept, truncated = _truncate_blocks([b1, huge, b3], budget)
    assert truncated and kept == [b1]


# ---------------------------------------------------------------------
# 端到端：图片按结构位置穿插 + 轮播归并
# ---------------------------------------------------------------------
def test_build_model_facing_html_places_images_at_structure_position():
    html = (
        "<html><head><title>结构测试</title></head><body>"
        "<p>第一段：介绍性文字，足够长以通过锚定阈值检验机制。</p>"
        '<img src="https://example.com/pic/mid-1.jpg"/>'
        "<p>第二段：展开论述的文字，同样足够长以通过匹配阈值检验。</p>"
        '<img src="https://example.com/pic/mid-2.jpg"/>'
        "<p>第三段：收尾总结的文字内容，仍然足够长以通过锚定阈值。</p>"
        "</body></html>"
    )
    # 正文块与 DOM 文本一致 → 文本锚定成功 → 图片应精确回到原始位置
    body_blocks = [
        "<p>第一段：介绍性文字，足够长以通过锚定阈值检验机制。</p>",
        "<p>第二段：展开论述的文字，同样足够长以通过匹配阈值检验。</p>",
        "<p>第三段：收尾总结的文字内容，仍然足够长以通过锚定阈值。</p>",
    ]
    result = build_model_facing_html(BASE, html, body_blocks=body_blocks, title="结构测试")
    assert result is not None
    assert result.count("<img") == 2                      # 不限量：2 张全保留
    pos_p1 = result.find("第一段")
    pos_img1 = result.find("mid-1.jpg")
    pos_p2 = result.find("第二段")
    pos_img2 = result.find("mid-2.jpg")
    pos_p3 = result.find("第三段")
    assert 0 < pos_p1 < pos_img1 < pos_p2 < pos_img2 < pos_p3


def test_build_model_facing_html_zero_anchor_fallback_avoids_tail_dump():
    # 正文块文本与 DOM 完全不同（零锚点）→ 比例兑底 → 图片穿插而非堆尾
    html = (
        "<html><body>"
        "<p>网页原始段落甲，内容与提取块完全不同足够长。</p>"
        '<img src="https://example.com/pic/fb-1.jpg"/>'
        "<p>网页原始段落乙，内容与提取块完全不同足够长。</p>"
        '<img src="https://example.com/pic/fb-2.jpg"/>'
        "<p>网页原始段落丙，内容与提取块完全不同足够长。</p>"
        "</body></html>"
    )
    body_blocks = [
        "<p>Alpha block text totally different from the DOM source.</p>",
        "<p>Beta block text totally different from the DOM source.</p>",
        "<p>Gamma block text totally different from the DOM source.</p>",
    ]
    result = build_model_facing_html(BASE, html, body_blocks=body_blocks, title="t")
    assert result is not None
    assert result.count("<img") == 2
    assert result.find("fb-1.jpg") < result.rfind("Gamma")  # 图片不在所有文本之后
    assert result.find("fb-1.jpg") < result.find("fb-2.jpg")  # 文档顺序保持


def test_build_model_facing_html_groups_carousel_into_slideshow():
    urls = [f"https://example.com/slide-{i}.jpg" for i in range(3)]
    dom = (
        "<html><body><p>图集页面的说明文字，足够长以通过锚定阈值检验机制。</p>"
        '<div class="swiper">'
        + "".join(f'<img src="{u}"/>' for u in urls)
        + "</div></body></html>"
    )
    body_blocks = [f'<img src="{u}"/>' for u in urls]
    result = build_model_facing_html(BASE, dom, body_blocks=body_blocks, title="图集")
    assert result is not None
    assert result.count("<tg-slideshow>") == 1
    assert result.count("<img") == 3
    slide = result[result.find("<tg-slideshow>"):result.find("</tg-slideshow>")]
    for u in urls:
        assert u in slide


# ---------------------------------------------------------------------
# 标题 / 兜底文本
# ---------------------------------------------------------------------
def test_extract_title_from_html_prefers_og_title():
    html = (
        "<html><head>"
        '<meta property="og:title" content="OG 标题"/>'
        "<title>标签标题</title></head><body></body></html>"
    )
    assert extract_title_from_html(html) == "OG 标题"


def test_build_fallback_text_skips_cookie_banner_and_nav_paragraphs():
    html = (
        "<html><body>"
        '<nav><p>网站导航栏目首页分类关于我们联系方式这一段很长。</p></nav>'
        '<div class="cookie-consent-banner"><p>我们使用 Cookie 来改善您的浏览体验并分析流量。</p></div>'
        "<p>这是正文第一段，介绍文章的主题背景与核心观点，长度足够。</p>"
        '<footer><p>版权所有 © 2024 示例网站保留所有权利备案例号。</p></footer>'
        "</body></html>"
    )
    text = build_fallback_text_from_html(html)
    assert "正文第一段" in text
    assert "Cookie" not in text
    assert "网站导航栏目" not in text
    assert "版权所有" not in text
