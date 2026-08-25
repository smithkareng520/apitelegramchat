# tests/test_fetch_rich_content.py — fetch_url 面向模型的 Telegram HTML 提取引擎测试
#
# 运行：PYTHONPATH=src python -m unittest discover -s tests -v
# 全部为离线测试（不访问网络），覆盖：
#   1. URL 安全过滤与 srcset 解析
#   2. iframe/embed 播放器规范化（YouTube/Vimeo/Bilibili 等）
#   3. DOM 媒体收集（带文档位置：video/audio/iframe/懒加载图、隐藏过滤、
#      装饰图过滤、去重、上限、轮播容器识别）
#   4. trafilatura XML → Telegram HTML 转换（全元素 + 容器行内合并 + 碎片过滤）
#   5. 面向模型的结果组装（媒体原位插入、轮播 slideshow、无聚合媒体区、
#      内容容器边界、整块截断闭合、重复标题去除、纯文本兜底、预算感知压缩）
#   6. search_engine.execute_fetch_url 集成（monkeypatch 网络）
import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apitelegramchat.fetch_rich_content import (
    FETCH_RICH_MAX_LEN,
    _collect_dom_media,
    _parse_dom,
    _truncate_blocks,
    build_fallback_text_from_html,
    build_model_facing_html,
    extract_title_from_html,
    trafilatura_xml_to_rich_html,
    _canonicalize_embed,
    _pick_srcset_best,
    _sanitize_url,
)


def run_async(coro):
    return asyncio.run(coro)


def collect(html, base="https://example.com/page"):
    tree = _parse_dom(html)
    assert tree is not None, "DOM 解析失败"
    return _collect_dom_media(tree, base)


class TestUrlHelpers(unittest.TestCase):
    def test_sanitize_rejects_dangerous_schemes(self):
        for bad in ("javascript:alert(1)", "data:text/html;base64,xxx", "blob:xyz", "vbscript:x", "#anchor"):
            self.assertIsNone(_sanitize_url(bad))

    def test_sanitize_resolves_relative_and_strips_fragment(self):
        self.assertEqual(
            _sanitize_url("/img/a.png", "https://example.com/post/1"),
            "https://example.com/img/a.png",
        )
        self.assertEqual(
            _sanitize_url("https://example.com/x?a=1#frag"),
            "https://example.com/x?a=1",
        )

    def test_sanitize_rejects_non_http(self):
        self.assertIsNone(_sanitize_url("ftp://example.com/file"))
        self.assertIsNone(_sanitize_url("not a url"))

    def test_srcset_best_pick(self):
        srcset = "a.jpg 480w, b.jpg 1080w, c.jpg 2x"
        # 2x 描述符换算为 2000，高于 1080w。
        self.assertEqual(_pick_srcset_best(srcset), "c.jpg")
        self.assertEqual(_pick_srcset_best("a.jpg 480w, b.jpg 1080w"), "b.jpg")


class TestEmbedCanonicalization(unittest.TestCase):
    def test_youtube_embed_to_watch(self):
        url, label = _canonicalize_embed(
            "https://www.youtube.com/embed/dQw4w9WgXcQ", "https://example.com"
        )
        self.assertEqual(url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(label, "YouTube")

    def test_youtube_nocookie(self):
        url, label = _canonicalize_embed(
            "https://www.youtube-nocookie.com/embed/abc123XYZ-_",
            "https://example.com",
        )
        self.assertEqual(url, "https://www.youtube.com/watch?v=abc123XYZ-_")

    def test_vimeo(self):
        url, label = _canonicalize_embed("https://player.vimeo.com/video/123456", "")
        self.assertEqual(url, "https://vimeo.com/123456")
        self.assertEqual(label, "Vimeo")

    def test_bilibili_bvid(self):
        url, label = _canonicalize_embed(
            "https://player.bilibili.com/player.html?bvid=BV1xx411c7mD&page=1", ""
        )
        self.assertEqual(url, "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(label, "Bilibili")

    def test_bilibili_aid(self):
        url, _ = _canonicalize_embed(
            "https://www.bilibili.com/blackboard/html5player.html?aid=170001", ""
        )
        self.assertEqual(url, "https://www.bilibili.com/video/av170001")

    def test_unknown_iframe_still_listed(self):
        url, label = _canonicalize_embed("https://unknown.example.com/frame", "")
        self.assertEqual(url, "https://unknown.example.com/frame")
        self.assertEqual(label, "嵌入内容")

    def test_javascript_iframe_rejected(self):
        self.assertIsNone(_canonicalize_embed("javascript:alert(1)", ""))


class TestDomMediaCollection(unittest.TestCase):
    def test_video_and_source_with_position(self):
        html = """
        <html><body><p>段落文本足够长。</p>
        <video controls><source src="https://cdn.example.com/clip.mp4" type="video/mp4"/></video>
        <p>之后的段落文本。</p></body></html>
        """
        media = collect(html)
        videos = [m for m in media if m.kind == "video"]
        self.assertEqual([v.url for v in videos], ["https://cdn.example.com/clip.mp4"])
        # 位置信息存在且在两个段落之间（用图片/视频 order 与段落对比由组装层保证）。
        self.assertGreater(videos[0].order_idx, 0)
        self.assertTrue(videos[0].path)

    def test_audio_extraction(self):
        html = '<html><body><audio><source src="/a/podcast.mp3" type="audio/mpeg"/></audio></body></html>'
        media = collect(html)
        audios = [m for m in media if m.kind == "audio"]
        self.assertEqual([a.url for a in audios], ["https://example.com/a/podcast.mp3"])

    def test_iframe_canonicalized_with_provider(self):
        html = '<html><body><iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" title="Rick Astley"></iframe></body></html>'
        media = collect(html, "https://example.com")
        embeds = [m for m in media if m.kind == "embed"]
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(embeds[0].provider, "YouTube")
        self.assertEqual(embeds[0].label, "Rick Astley")

    def test_lazy_image_attrs_and_srcset(self):
        html = """
        <html><body>
        <img data-src="https://cdn.example.com/lazy.jpg"/>
        <img srcset="https://cdn.example.com/s.jpg 480w, https://cdn.example.com/l.jpg 1200w"/>
        </body></html>
        """
        media = collect(html, "https://example.com")
        urls = [m.url for m in media if m.kind == "image"]
        self.assertIn("https://cdn.example.com/lazy.jpg", urls)
        self.assertIn("https://cdn.example.com/l.jpg", urls)
        self.assertNotIn("https://cdn.example.com/s.jpg", urls)

    def test_figcaption_as_label(self):
        html = '<html><body><figure><img src="https://cdn.example.com/p.jpg"/><figcaption>图片说明文字</figcaption></figure></body></html>'
        media = collect(html)
        imgs = [m for m in media if m.kind == "image"]
        self.assertEqual(imgs[0].label, "图片说明文字")

    def test_decorative_images_filtered(self):
        html = """
        <html><body>
        <img src="https://cdn.example.com/spacer.gif"/>
        <img src="https://cdn.example.com/icon-share.png"/>
        <img src="https://cdn.example.com/real-photo.jpg"/>
        </body></html>
        """
        media = collect(html)
        urls = [m.url for m in media if m.kind == "image"]
        self.assertEqual(urls, ["https://cdn.example.com/real-photo.jpg"])

    def test_hidden_media_skipped(self):
        html = """
        <html><body>
        <iframe src="https://ads.example.com/tracker" style="display:none"></iframe>
        <img src="https://cdn.example.com/pixel.jpg" width="0" height="0"/>
        <video src="https://cdn.example.com/v.mp4"></video>
        </body></html>
        """
        media = collect(html)
        urls = [m.url for m in media]
        self.assertEqual(urls, ["https://cdn.example.com/v.mp4"])

    def test_dedupe_and_limits(self):
        imgs = "".join(f'<img src="https://cdn.example.com/p{i}.jpg"/>' for i in range(20))
        html = f"<html><body>{imgs}</body></html>"
        media = collect(html)
        self.assertEqual(len([m for m in media if m.kind == "image"]), 8)  # MAX_IMAGES

    def test_relative_urls_resolved(self):
        html = '<html><body><img src="img/photo.png"/></body></html>'
        media = collect(html, "https://example.com/articles/2024/x.html")
        self.assertEqual([m.url for m in media], ["https://example.com/articles/2024/img/photo.png"])

    def test_carousel_detection_shared_container(self):
        # swiper 结构：每张图共享外层 .swiper 容器（不是各自的 .swiper-slide）。
        html = """
        <html><body><article>
        <div class="swiper"><div class="swiper-slide"><img src="https://cdn.example.com/a.jpg"/></div>
        <div class="swiper-slide"><img src="https://cdn.example.com/b.jpg"/></div>
        <div class="swiper-slide"><img src="https://cdn.example.com/c.jpg"/></div></div>
        <img src="https://cdn.example.com/plain.jpg"/>
        </article></body></html>
        """
        media = collect(html)
        carousel_paths = {m.url.rsplit("/", 1)[-1]: m.carousel for m in media if m.kind == "image"}
        self.assertIsNotNone(carousel_paths["a.jpg"])
        self.assertEqual(carousel_paths["a.jpg"], carousel_paths["b.jpg"])
        self.assertEqual(carousel_paths["b.jpg"], carousel_paths["c.jpg"])
        self.assertIsNone(carousel_paths["plain.jpg"])

    def test_og_meta_not_collected_as_media(self):
        # og:image 是元数据而非文档流元素 → 不收集（忠实文档顺序原则）。
        html = ('<html><head><meta property="og:image" content="https://cdn.example.com/og.jpg"/>'
                '<meta property="og:video" content="https://cdn.example.com/ogv.mp4"/></head>'
                '<body><p>正文段落。</p></body></html>')
        media = collect(html)
        self.assertEqual(media, [])


class TestXmlToRichHtml(unittest.TestCase):
    # 注意：这里的 XML 必须是 trafilatura 的真实输出标记——
    # 行内格式是 <hi rend="#b">…</hi>，链接是 <ref target="…">…</ref>。
    XML = """<doc fingerprint="x"><main>
    <head rend="h1">标题一</head>
    <p>这是<hi rend="#b">粗体</hi>与<hi rend="#i">斜体</hi>、<ref target="https://example.com/ref">链接</ref>与<code>行内码</code>。</p>
    <graphic src="https://cdn.example.com/pic.png" alt="示例图"/>
    <list rend="ul"><item>项目甲</item><item>项目乙 <hi rend="#b">强调</hi></item></list>
    <list rend="ol"><item>第一</item><item>第二</item></list>
    <quote>引用文本</quote>
    <code>print("hi")</code>
    <table><row><cell role="head">列A</cell><cell role="head">列B</cell></row>
    <row><cell colspan="2">合并单元格</cell></row></table>
    <p>段落里有内嵌图 <graphic src="https://cdn.example.com/inline.png"/> 后续文字。</p>
    </main></doc>"""

    def setUp(self):
        self.blocks = trafilatura_xml_to_rich_html(self.XML, "https://example.com/page")

    def test_heading_and_paragraph(self):
        self.assertIn("<h1>标题一</h1>", self.blocks)
        para = next(b for b in self.blocks if b.startswith("<p>这是"))
        self.assertIn("<b>粗体</b>", para)
        self.assertIn("<i>斜体</i>", para)
        self.assertIn('<a href="https://example.com/ref">链接</a>', para)
        self.assertIn("<code>行内码</code>", para)

    def test_heading_levels_preserved(self):
        # trafilatura 用 <head rend="hN"> 保留标题级别，必须 1:1 映射到 <hN>。
        xml = ('<doc><main>'
               '<head rend="h1">一级</head><head rend="h2">二级</head>'
               '<head rend="h3">三级</head><head rend="h4">四级</head>'
               '<head rend="h5">五级</head><head rend="h6">六级</head>'
               '<head>无级别默认二级</head>'
               '</main></doc>')
        blocks = trafilatura_xml_to_rich_html(xml, "")
        for n, cn in enumerate("一二三四五六", start=1):
            self.assertIn(f"<h{n}>{cn}级</h{n}>", blocks)
        self.assertIn("<h2>无级别默认二级</h2>", blocks)

    def test_del_becomes_strikethrough(self):
        # trafilatura 把 <s>/<del>/<strike> 统一转成 <del>（删除线）。
        xml = ('<doc><main><p>正常<del>已删除</del>与<del rend="overstrike">旧价</del>文字</p></main></doc>')
        blocks = trafilatura_xml_to_rich_html(xml, "")
        para = next(b for b in blocks if "正常" in b)
        self.assertIn("<s>已删除</s>", para)
        self.assertIn("<s>旧价</s>", para)

    def test_teletype_rend_becomes_code(self):
        # <kbd>/<samp>/<tt>/<var> → <hi rend="#t">（等宽）→ <code>。
        xml = '<doc><main><p>按<hi rend="#t">Ctrl</hi>与<hi rend="#t">Enter</hi></p></main></doc>'
        blocks = trafilatura_xml_to_rich_html(xml, "")
        para = next(b for b in blocks if "按" in b)
        self.assertIn("<code>Ctrl</code>", para)
        self.assertIn("<code>Enter</code>", para)

    def test_graphic_becomes_block_img(self):
        img_blocks = [b for b in self.blocks if "<img" in b]
        self.assertTrue(any('<figure><img src="https://cdn.example.com/pic.png"/>' in b for b in img_blocks))

    def test_lists(self):
        self.assertIn("<ul><li>项目甲</li><li>项目乙 <b>强调</b></li></ul>", self.blocks)
        self.assertIn("<ol><li>第一</li><li>第二</li></ol>", self.blocks)

    def test_quote_and_code(self):
        self.assertIn("<blockquote>引用文本</blockquote>", self.blocks)
        self.assertIn('<pre><code>print("hi")</code></pre>', self.blocks)

    def test_table(self):
        table = next(b for b in self.blocks if b.startswith("<table"))
        self.assertIn('<table bordered striped>', table)
        self.assertIn("<td><b>列A</b></td>", table)
        self.assertIn('<td colspan="2">合并单元格</td>', table)

    def test_media_lifted_out_of_paragraph(self):
        # 段落中的 <graphic> 必须提升为兄弟块，绝不能留在 <p> 内。
        para = next(b for b in self.blocks if "段落里有内嵌图" in b)
        self.assertNotIn("<img", para)
        self.assertTrue(any(
            b == '<img src="https://cdn.example.com/inline.png"/>' for b in self.blocks
        ))

    def test_invalid_xml_returns_empty(self):
        self.assertEqual(trafilatura_xml_to_rich_html("<broken<<", ""), [])
        self.assertEqual(trafilatura_xml_to_rich_html("", ""), [])

    def test_xml_escaping(self):
        xml = '<doc><main><p>a &lt; b &amp; c</p></main></doc>'
        blocks = trafilatura_xml_to_rich_html(xml, "")
        self.assertIn("<p>a &lt; b &amp; c</p>", blocks)

    def test_container_inline_children_merged(self):
        # 维基百科 favor_precision 场景：<ref> 直接挂在 <main> 下，尾巴文本
        # 散落为 ：、等碎片 → 必须合并成一个段落，而不是 N 个碎片段落。
        xml = (
            '<doc><main>'
            '<p><ref target="/wiki/paradigm">编程范型</ref></p>'
            '<ref target="/wiki/multi">多范型</ref>：'
            '<ref target="/wiki/proc">过程式</ref>、'
            '<ref target="/wiki/oop">面向对象</ref>、'
            '<ref target="/wiki/func">函数式</ref>'
            '<p><ref target="/wiki/license">许可证</ref></p>'
            '<ref target="/wiki/psf">Python软件基金会许可证</ref>'
            '</main></doc>'
        )
        blocks = trafilatura_xml_to_rich_html(xml, "https://zh.wikipedia.org/wiki/X")
        self.assertNotIn("<p>：</p>", blocks)
        self.assertNotIn("<p>、</p>", blocks)
        merged = next(b for b in blocks if "多范型" in b)
        self.assertIn('<a href="https://zh.wikipedia.org/wiki/multi">多范型</a>', merged)
        self.assertIn('<a href="https://zh.wikipedia.org/wiki/proc">过程式</a>', merged)
        self.assertIn("：", merged)
        self.assertIn("、", merged)

    def test_punct_only_paragraphs_dropped(self):
        xml = '<doc><main><p>：</p><p>、</p><p>正文段落保留</p></main></doc>'
        blocks = trafilatura_xml_to_rich_html(xml, "")
        self.assertNotIn("<p>：</p>", blocks)
        self.assertNotIn("<p>、</p>", blocks)
        self.assertIn("<p>正文段落保留</p>", blocks)

    def test_empty_table_dropped(self):
        xml = ('<doc><main><table><row><cell></cell></row></table>'
               '<table><row><cell role="head">名称</cell><cell>Python</cell></row></table></main></doc>')
        blocks = trafilatura_xml_to_rich_html(xml, "")
        tables = [b for b in blocks if b.startswith("<table")]
        self.assertEqual(len(tables), 1)
        self.assertIn("Python", tables[0])


class TestTruncateBlocks(unittest.TestCase):
    def test_truncate_closes_blocks(self):
        blocks = [f"<p>{'x' * 100}</p>" for _ in range(10)]
        kept, truncated = _truncate_blocks(blocks, 350)
        self.assertTrue(truncated)
        total = sum(len(b) + 1 for b in kept)
        self.assertLessEqual(total, 350)
        for b in kept:
            self.assertTrue(b.startswith("<p>") and b.endswith("</p>"))

    def test_no_truncation(self):
        blocks = ["<p>short</p>"]
        kept, truncated = _truncate_blocks(blocks, 100)
        self.assertEqual(kept, blocks)
        self.assertFalse(truncated)

    def test_body_budget_leaves_header_headroom(self):
        # 正文预算必须给页头（标题 + 来源链接）与截断提示留出余量，
        # 否则二级截断会切掉正文尾部。历史教训：11000 → 13600 修复。
        from apitelegramchat import fetch_rich_content as _F
        self.assertGreater(_F.FETCH_RICH_MAX_LEN, _F.FETCH_BODY_MAX_LEN + 400)
        self.assertLess(_F.FETCH_BODY_MAX_LEN + 600, 16000)  # MAX_TOOL_RESPONSE_LEN


_WIKI = "https://zh.wikipedia.org"
_EP_TABLE = (
    '<table bordered striped><tr><td><b>話數</b></td><td><b>日文標題</b></td></tr>'
    '<tr><td>第1話</td><td>はじめてのパートナー</td></tr>'
    '<tr><td>第2話</td><td>足を引っ張りたくないので</td></tr>'
    '<tr><td>第3話</td><td>同棲はじめました</td></tr></table>'
)


def _linky_paragraph(i: int) -> str:
    """生成一段带 5 个维基百科内链 + 1 个跨域链接的正文块（回归背景：
    维基内链 URL 百分号编码，单条 ~85 字符，数百条即可吃掉 40% 预算）。"""
    links = "".join(
        f'<a href="{_WIKI}/wiki/%E6%9D%A1%E7%9B%AE{i}{j}">內鏈{j}</a>' for j in range(5)
    )
    return (
        f"<p>第{i}段：{links}与<a href=\"https://www.example.org/promo\">官方預告</a>"
        f"等內容，段落長度足夠以進行測試。</p>"
    )


class TestBudgetCompaction(unittest.TestCase):
    """预算感知压缩：超预算时同源链接降级为纯文本，靠后的表格得以保留。"""

    def test_demote_same_origin_only(self):
        from apitelegramchat.fetch_rich_content import _demote_same_origin_links
        blocks = [
            '<p>參見<a href="https://zh.wikipedia.org/wiki/%E8%97%A4%E5%8E%9F">藤原佳幸</a>'
            '與<a href="https://www.example.org/x"><b>跨域加粗</b></a>。</p>'
        ]
        out = _demote_same_origin_links(blocks, "https://zh.wikipedia.org/wiki/頁面")
        # 同源 → 纯锚文本；跨域 → 完整保留。
        self.assertIn("藤原佳幸", out[0])
        self.assertNotIn('href="https://zh.wikipedia.org/wiki/%E8%97%A4%E5%8E%9F"', out[0])
        self.assertIn('<a href="https://www.example.org/x"><b>跨域加粗</b></a>', out[0])
        # 可见文本无损。
        strip = lambda s: re.sub(r"<[^>]+>", "", s)
        self.assertEqual(strip(blocks[0]), strip(out[0]))

    def test_no_demotion_within_budget(self):
        # 内容在预算内 → 链接原样保留（保真优先）。
        html = "<html><head><title>T</title></head><body><article><p>正文。</p></article></body></html>"
        blocks = [_linky_paragraph(1), _EP_TABLE]
        result = build_model_facing_html(
            "https://zh.wikipedia.org/wiki/X", html, body_blocks=blocks, title="T"
        )
        self.assertIn('<a href="https://zh.wikipedia.org/wiki/%E6%9D%A1%E7%9B%AE10">內鏈0</a>', result)
        self.assertIn("第1話", result)

    def test_late_table_survives_budget(self):
        # 回归测试（真实 bug：zh.wikipedia 可塑性記憶 各話列表被截断）：
        # 内链膨胀导致总长超预算时，压缩同源链接而不是截掉靠后的表格。
        from unittest import mock
        from apitelegramchat import fetch_rich_content as F

        html = "<html><head><title>T</title></head><body><article><p>正文。</p></article></body></html>"
        blocks = [_linky_paragraph(i) for i in range(1, 7)] + [_EP_TABLE, "<p>結尾段落。</p>"]
        pre = sum(len(b) + 1 for b in blocks)
        self.assertGreater(pre, 2000)  # 前置条件：确实超预算

        with mock.patch.object(F, "FETCH_BODY_MAX_LEN", 2000):
            result = F.build_model_facing_html(
                "https://zh.wikipedia.org/wiki/X", html, body_blocks=blocks, title="T"
            )
        # 每话表格完整保留（压缩前它位于预算之外）。
        for ep in ("第1話", "第2話", "第3話",
                   "はじめてのパートナー", "足を引っ張りたくないので", "同棲はじめました"):
            self.assertIn(ep, result)
        # 同源链接已降级、跨域链接保留、页头来源链接不受影响。
        self.assertNotIn('href="https://zh.wikipedia.org/wiki/%E6%9D%A1%E7%9B%AE10"', result)
        self.assertIn('<a href="https://www.example.org/promo">官方預告</a>', result)
        self.assertIn('<a href="https://zh.wikipedia.org/wiki/X">zh.wikipedia.org</a>', result)
        # 总长受控。
        self.assertLessEqual(len(result), F.FETCH_RICH_MAX_LEN)


class TestModelFacingHtml(unittest.TestCase):
    """面向模型的结果组装：媒体原位、轮播 slideshow、无聚合媒体区。"""

    def test_header_and_basic_structure(self):
        html = "<html><head><title>T</title></head><body><article><p>唯一正文段落内容。</p></article></body></html>"
        result = build_model_facing_html(
            "https://example.com/post", html, body_blocks=["<p>唯一正文段落内容。</p>"], title="页面标题"
        )
        self.assertIn("<h3>页面标题</h3>", result)
        self.assertIn('<p>🔗 <a href="https://example.com/post">example.com</a></p>', result)
        self.assertIn("<p>唯一正文段落内容。</p>", result)
        # 无聚合媒体区标题。
        for marker in ("🎬", "📺", "🎵", "🖼️"):
            self.assertNotIn(marker, result)

    def test_media_at_original_positions(self):
        html = """<html><head><title>T</title></head><body><article>
        <p>这是第一段正文内容足够长。</p>
        <video><source src="/media/clip.mp4" type="video/mp4"/></video>
        <p>这是第二段正文内容也是足够长的。</p>
        </article></body></html>"""
        blocks = [
            "<p>这是第一段正文内容足够长。</p>",
            "<p>这是第二段正文内容也是足够长的。</p>",
        ]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="T")
        self.assertIn('<video src="https://example.com/media/clip.mp4"/>', result)
        # 顺序：第一段 < 视频 < 第二段。
        self.assertLess(result.index("第一段"), result.index("clip.mp4"))
        self.assertLess(result.index("clip.mp4"), result.index("第二段"))

    def test_embed_link_at_original_position(self):
        html = """<html><head><title>T</title></head><body><article>
        <p>第一段落正文内容足够长了。</p>
        <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" title="Demo"></iframe>
        <p>第二段落正文内容足够长了。</p>
        </article></body></html>"""
        blocks = ["<p>第一段落正文内容足够长了。</p>", "<p>第二段落正文内容足够长了。</p>"]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="T")
        self.assertIn('<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">YouTube · Demo</a>', result)
        self.assertLess(result.index("第一段落"), result.index("YouTube"))
        self.assertLess(result.index("YouTube"), result.index("第二段落"))

    def test_dropped_lazy_image_inserted_at_position(self):
        html = """<html><head><title>T</title></head><body><article>
        <p>第一段落正文内容足够长了。</p>
        <img data-src="https://cdn.example.com/lazy.png" alt="懒图"/>
        <p>第二段落正文内容足够长了。</p>
        </article></body></html>"""
        blocks = ["<p>第一段落正文内容足够长了。</p>", "<p>第二段落正文内容足够长了。</p>"]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="T")
        self.assertIn('<figure><img src="https://cdn.example.com/lazy.png"/><figcaption>懒图</figcaption></figure>', result)
        self.assertLess(result.index("第一段落"), result.index("lazy.png"))
        self.assertLess(result.index("lazy.png"), result.index("第二段落"))

    def test_kept_image_block_reanchored_to_dom_position(self):
        # trafilatura 把 graphic 放在段落后的 XML 末尾，但 DOM 中图片位于
        # 两段之间 → 通过 URL 锚定回到原始位置。
        html = """<html><head><title>T</title></head><body><article>
        <p>第一段落正文内容足够长了。</p>
        <img src="https://cdn.example.com/mid.png"/>
        <p>第二段落正文内容足够长了。</p>
        </article></body></html>"""
        blocks = [
            "<p>第一段落正文内容足够长了。</p>",
            "<p>第二段落正文内容足够长了。</p>",
            '<img src="https://cdn.example.com/mid.png"/>',
        ]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="T")
        self.assertLess(result.index("第一段落"), result.index("mid.png"))
        self.assertLess(result.index("mid.png"), result.index("第二段落"))

    def test_carousel_run_grouping_in_place(self):
        # trafilatura 保留了轮播三图（连续 img 块）→ 合并为 slideshow，位置不变。
        html = """<html><head><title>T</title></head><body><article>
        <p>intro paragraph text long enough here.</p>
        <div class="swiper"><div class="swiper-slide"><img src="https://cdn.example.com/a.jpg"/></div>
        <div class="swiper-slide"><img src="https://cdn.example.com/b.jpg"/></div>
        <div class="swiper-slide"><img src="https://cdn.example.com/c.jpg"/></div></div>
        <p>outro paragraph text long enough here too.</p>
        </article></body></html>"""
        blocks = [
            "<p>intro paragraph text long enough here.</p>",
            '<img src="https://cdn.example.com/a.jpg"/>',
            '<img src="https://cdn.example.com/b.jpg"/>',
            '<img src="https://cdn.example.com/c.jpg"/>',
            "<p>outro paragraph text long enough here too.</p>",
        ]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="T")
        self.assertIn("<tg-slideshow>", result)
        self.assertEqual(result.count("<img"), 3)
        self.assertLess(result.index("intro"), result.index("<tg-slideshow>"))
        self.assertLess(result.index("<tg-slideshow>"), result.index("outro"))

    def test_all_lazy_carousel_slideshow_at_position(self):
        # 轮播图全部懒加载（trafilatura 全丢）→ 在轮播位置插入完整 slideshow。
        html = """<html><head><title>T</title></head><body><article>
        <p>第一段落正文内容足够长了。</p>
        <div class="product-gallery">
        <img data-src="https://cdn.example.com/g1.jpg"/>
        <img data-src="https://cdn.example.com/g2.jpg"/>
        <img data-src="https://cdn.example.com/g3.jpg"/>
        </div>
        <p>第二段落正文内容足够长了。</p>
        </article></body></html>"""
        blocks = ["<p>第一段落正文内容足够长了。</p>", "<p>第二段落正文内容足够长了。</p>"]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="T")
        self.assertIn("<tg-slideshow>", result)
        self.assertEqual(result.count("<img"), 3)
        self.assertLess(result.index("第一段落"), result.index("<tg-slideshow>"))
        self.assertLess(result.index("<tg-slideshow>"), result.index("第二段落"))

    def test_adjacent_images_without_carousel_not_grouped(self):
        # 无轮播容器特征的相邻图片保持独立 <img>（忠实原结构）。
        html = """<html><head><title>T</title></head><body><article>
        <p>第一段落正文内容足够长了。</p>
        <div><img src="https://cdn.example.com/x1.jpg"/><img src="https://cdn.example.com/x2.jpg"/></div>
        <p>第二段落正文内容足够长了。</p>
        </article></body></html>"""
        blocks = [
            "<p>第一段落正文内容足够长了。</p>",
            '<img src="https://cdn.example.com/x1.jpg"/>',
            '<img src="https://cdn.example.com/x2.jpg"/>',
            "<p>第二段落正文内容足够长了。</p>",
        ]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="T")
        self.assertNotIn("<tg-slideshow>", result)
        self.assertEqual(result.count("<img"), 2)

    def test_footer_media_excluded_by_content_boundary(self):
        # 页脚的 iframe/widget 不属于正文内容 → 不进入结果。
        html = """<html><head><title>T</title></head><body>
        <article><p>正文段落内容足够长了。</p></article>
        <footer><iframe src="https://social.example.com/widget"></iframe>
        <img src="https://cdn.example.com/footer-logo.png"/></footer>
        </body></html>"""
        blocks = ["<p>正文段落内容足够长了。</p>"]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="T")
        self.assertNotIn("social.example.com", result)
        self.assertNotIn("footer-logo", result)

    def test_no_aggregated_media_sections(self):
        html = """<html><head><title>T</title></head><body><article>
        <p>第一段落正文内容足够长了。</p>
        <video src="https://cdn.example.com/v.mp4"></video>
        <audio src="https://cdn.example.com/a.mp3"></audio>
        <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>
        <img src="https://cdn.example.com/i.jpg"/>
        <p>第二段落正文内容足够长了。</p>
        </article></body></html>"""
        blocks = ["<p>第一段落正文内容足够长了。</p>", "<p>第二段落正文内容足够长了。</p>"]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="T")
        for marker in ("🎬 视频", "📺 内嵌播放器", "🎵 音频", "🖼️ 图片"):
            self.assertNotIn(marker, result)
        # 媒体全部存在且在两段之间（原位）。
        self.assertIn('<video src="https://cdn.example.com/v.mp4"/>', result)
        self.assertIn('<audio src="https://cdn.example.com/a.mp3"/>', result)
        self.assertIn("YouTube", result)
        self.assertIn('src="https://cdn.example.com/i.jpg"', result)

    def test_duplicate_first_heading_removed(self):
        html = "<html><head><title>Same Title</title></head><body><article><h1>Same Title</h1><p>正文段落。</p></article></body></html>"
        result = build_model_facing_html(
            "https://example.com/p", html, body_blocks=["<h1>Same Title</h1>", "<p>正文段落。</p>"], title="Same Title"
        )
        self.assertNotIn("<h1>Same Title</h1>", result)
        self.assertIn("<h3>Same Title</h3>", result)

    def test_fallback_text_used_when_no_blocks(self):
        html = "<html><head><title>T</title></head><body><p>纯文本段落。</p></body></html>"
        result = build_model_facing_html(
            "https://example.com/p", html, body_blocks=[], title="标题", fallback_text="第一段。\n\n第二段。"
        )
        self.assertIn("<p>第一段。</p>", result)
        self.assertIn("<p>第二段。</p>", result)

    def test_long_content_truncated_at_block_boundary(self):
        html = "<html><head><title>T</title></head><body><article><p>正文段落。</p></article></body></html>"
        blocks = [f"<p>{'内容' * 900}</p>" for _ in range(30)]
        result = build_model_facing_html("https://example.com/p", html, body_blocks=blocks, title="标题")
        self.assertLessEqual(len(result), FETCH_RICH_MAX_LEN)
        self.assertIn("正文过长，已截断", result)
        self.assertEqual(result.count("<p>"), result.count("</p>"))

    def test_media_only_page(self):
        # 无正文、无兜底文本，但 DOM 有视频 → 仍产出结果（媒体型）。
        html = ('<html><head><title>Video</title></head><body>'
                '<video src="https://cdn.example.com/only.mp4"></video></body></html>')
        result = build_model_facing_html("https://example.com/v", html, body_blocks=[], title="Video")
        self.assertIsNotNone(result)
        self.assertIn('<video src="https://cdn.example.com/only.mp4"/>', result)

    def test_returns_none_when_nothing(self):
        html = "<html><head><title>T</title></head><body><div></div></body></html>"
        result = build_model_facing_html("https://example.com/e", html, body_blocks=[], title="")
        self.assertIsNone(result)


class TestTitleAndFallback(unittest.TestCase):
    def test_og_title_preferred(self):
        html = '<html><head><title>HTML 标题</title><meta property="og:title" content="OG 标题"/></head><body></body></html>'
        self.assertEqual(extract_title_from_html(html), "OG 标题")

    def test_plain_title(self):
        html = '<html><head><title>  纯 标题  </title></head><body></body></html>'
        self.assertEqual(extract_title_from_html(html), "纯 标题")

    def test_fallback_text(self):
        html = ('<html><head><meta name="description" content="页面描述"/></head>'
                '<body><p>这是第一段正文内容足够长。</p><p>第二段也足够长一些。</p></body></html>')
        text = build_fallback_text_from_html(html)
        self.assertIn("页面描述", text)
        self.assertIn("第一段正文", text)


class TestExecuteFetchUrlIntegration(unittest.TestCase):
    """集成测试：monkeypatch 网络层，验证 execute_fetch_url 的最终输出。"""

    PAGE_HTML = """
    <html><head><title>集成测试页面</title>
    <meta property="og:image" content="https://cdn.example.com/cover.jpg"/>
    </head><body><article>
    <h1>集成测试页面</h1>
    <p>这是一段足够长的正文内容，用来通过最短长度校验。包含<b>加粗</b>与
    <a href="https://example.com/more">更多阅读</a>链接，长度足够。</p>
    <p>第二段正文，同样足够长，确保 trafilatura 能稳定提取出有效正文内容。</p>
    <iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>
    <img src="https://cdn.example.com/photo.jpg" alt="新闻配图"/>
    </article></body></html>
    """

    def _patch_fetch(self, html, download=None):
        import apitelegramchat.search_engine as se
        original_curl = se._fetch_html_with_curl
        original_download = se._download_html_with_trafilatura

        async def fake_curl(url):
            return html

        async def fake_download(url):
            return download

        se._fetch_html_with_curl = fake_curl
        if download is not None:
            se._download_html_with_trafilatura = fake_download
        se._fetch_cache.clear()

        def restore():
            se._fetch_html_with_curl = original_curl
            se._download_html_with_trafilatura = original_download

        self.addCleanup(restore)
        return se

    def test_success_returns_telegram_html_in_document_order(self):
        se = self._patch_fetch(self.PAGE_HTML)
        result = run_async(se.execute_fetch_url("https://example.com/integration-test"))
        self.assertFalse(result.startswith("失败"))
        self.assertIn("<h3>集成测试页面</h3>", result)
        self.assertIn('<p>🔗 <a href="https://example.com/integration-test">example.com</a></p>', result)
        self.assertIn("<b>加粗</b>", result)
        self.assertIn('<a href="https://example.com/more">更多阅读</a>', result)
        # 媒体在原始位置：正文 → YouTube 链接 → 图片（DOM 顺序）。
        self.assertIn('<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">YouTube</a>', result)
        self.assertIn('src="https://cdn.example.com/photo.jpg"', result)
        body_pos = result.index("足够长的正文内容")
        yt_pos = result.index("YouTube")
        img_pos = result.index("photo.jpg")
        self.assertLess(body_pos, yt_pos)
        self.assertLess(yt_pos, img_pos)
        # 无聚合媒体区。
        for marker in ("🎬 视频", "📺 内嵌播放器", "🖼️ 图片", "🎵 音频"):
            self.assertNotIn(marker, result)
        # og:image 元数据不进入文档流。
        self.assertNotIn("cover.jpg", result)

    def test_failure_not_cached_and_prefix_preserved(self):
        se = self._patch_fetch(None, download=None)
        result = run_async(se.execute_fetch_url("https://example.com/empty-page"))
        self.assertTrue(result.startswith("失败："))
        self.assertIsNone(se.get_fetch_cache("https://example.com/empty-page"))

    def test_ssrf_rejected(self):
        se = self._patch_fetch(self.PAGE_HTML)
        result = run_async(se.execute_fetch_url("file:///etc/passwd"))
        self.assertTrue(result.startswith("失败："))

    def test_success_cached(self):
        se = self._patch_fetch(self.PAGE_HTML)
        run_async(se.execute_fetch_url("https://example.com/cache-test"))
        cached = se.get_fetch_cache("https://example.com/cache-test")
        self.assertIsNotNone(cached)
        self.assertIn("<h3>", cached)


if __name__ == "__main__":
    unittest.main()
