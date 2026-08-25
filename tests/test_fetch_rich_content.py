# tests/test_fetch_rich_content.py — fetch_url Telegram Rich HTML 提取引擎测试
#
# 运行：PYTHONPATH=src python -m unittest discover -s tests -v
# 全部为离线测试（不访问网络），覆盖：
#   1. trafilatura XML → Telegram HTML 转换（段落/格式/链接/列表/引用/代码/表格/媒体提升）
#   2. 嵌入媒体提取（<video>/<source>、iframe 播放器规范化、懒加载图片、OG、JSON-LD、
#      危险协议过滤、装饰图过滤、去重、数量上限）
#   3. 结果组装（预算截断闭合标签、正文内媒体去重、重复标题去除、纯文本兜底）
#   4. search_engine.execute_fetch_url 集成（monkeypatch 网络，验证输出格式与失败路径）
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apitelegramchat.fetch_rich_content import (
    FETCH_RICH_MAX_LEN,
    MediaAsset,
    PageMedia,
    _canonicalize_embed,
    _pick_srcset_best,
    _sanitize_url,
    _truncate_blocks,
    build_fetch_rich_result,
    build_fallback_text_from_html,
    extract_embedded_media,
    extract_title_from_html,
    trafilatura_xml_to_rich_html,
)


def run_async(coro):
    return asyncio.run(coro)


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


class TestMediaExtraction(unittest.TestCase):
    def test_video_and_source(self):
        html = """
        <html><body>
        <video controls poster="https://cdn.example.com/poster.jpg">
            <source src="https://cdn.example.com/clip.mp4" type="video/mp4"/>
        </video>
        </body></html>
        """
        media = extract_embedded_media(html, "https://example.com/watch")
        self.assertEqual([v.url for v in media.videos], ["https://cdn.example.com/clip.mp4"])
        self.assertIn("https://cdn.example.com/poster.jpg", [i.url for i in media.images])

    def test_audio_extraction(self):
        html = '<html><body><audio><source src="/a/podcast.mp3" type="audio/mpeg"/></audio></body></html>'
        media = extract_embedded_media(html, "https://example.com/page")
        self.assertEqual([a.url for a in media.audios], ["https://example.com/a/podcast.mp3"])

    def test_iframe_extraction(self):
        html = '<html><body><iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ" title="Rick Astley"></iframe></body></html>'
        media = extract_embedded_media(html, "https://example.com")
        self.assertEqual(len(media.embeds), 1)
        self.assertEqual(media.embeds[0].url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(media.embeds[0].provider, "YouTube")
        self.assertEqual(media.embeds[0].label, "Rick Astley")

    def test_og_media(self):
        html = """
        <html><head>
        <meta property="og:video" content="https://media.example.com/v.mp4"/>
        <meta property="og:image" content="https://media.example.com/cover.jpg"/>
        <meta property="og:title" content="OG 标题"/>
        <meta property="og:description" content="OG 描述"/>
        </head><body></body></html>
        """
        media = extract_embedded_media(html, "https://example.com")
        self.assertEqual([v.url for v in media.videos], ["https://media.example.com/v.mp4"])
        self.assertIn("https://media.example.com/cover.jpg", [i.url for i in media.images])
        self.assertEqual(media.og_title, "OG 标题")
        self.assertEqual(media.og_description, "OG 描述")

    def test_jsonld_video_object(self):
        html = """
        <html><head><script type="application/ld+json">
        {"@type": "VideoObject", "name": "新闻回顾",
         "contentUrl": "https://cdn.example.com/news.mp4",
         "thumbnailUrl": "https://cdn.example.com/thumb.jpg"}
        </script></head><body></body></html>
        """
        media = extract_embedded_media(html, "https://example.com")
        self.assertEqual([v.url for v in media.videos], ["https://cdn.example.com/news.mp4"])
        self.assertEqual(media.videos[0].label, "新闻回顾")

    def test_lazy_image_attrs_and_srcset(self):
        html = """
        <html><body>
        <img data-src="https://cdn.example.com/lazy.jpg"/>
        <img srcset="https://cdn.example.com/s.jpg 480w, https://cdn.example.com/l.jpg 1200w"/>
        </body></html>
        """
        media = extract_embedded_media(html, "https://example.com")
        urls = [i.url for i in media.images]
        self.assertIn("https://cdn.example.com/lazy.jpg", urls)
        self.assertIn("https://cdn.example.com/l.jpg", urls)
        self.assertNotIn("https://cdn.example.com/s.jpg", urls)

    def test_decorative_images_filtered(self):
        html = """
        <html><body>
        <img src="https://cdn.example.com/spacer.gif"/>
        <img src="https://cdn.example.com/icon-share.png"/>
        <img src="https://cdn.example.com/real-photo.jpg"/>
        </body></html>
        """
        media = extract_embedded_media(html, "https://example.com")
        urls = [i.url for i in media.images]
        self.assertEqual(urls, ["https://cdn.example.com/real-photo.jpg"])

    def test_dedupe_and_limits(self):
        imgs = "".join(
            f'<img src="https://cdn.example.com/p{i}.jpg"/>' for i in range(20)
        )
        html = f"<html><body>{imgs}</body></html>"
        media = extract_embedded_media(html, "https://example.com")
        self.assertEqual(len(media.images), 8)  # MAX_IMAGES

    def test_relative_urls_resolved(self):
        html = '<html><body><img src="img/photo.png"/></body></html>'
        media = extract_embedded_media(html, "https://example.com/articles/2024/x.html")
        self.assertEqual([i.url for i in media.images], ["https://example.com/articles/2024/img/photo.png"])


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
        # 碎片段落被合并：不再出现单独的 <p>：</p>。
        self.assertNotIn("<p>：</p>", blocks)
        self.assertNotIn("<p>、</p>", blocks)
        # 合并后的段落包含完整的行内序列。
        merged = next(b for b in blocks if "多范型" in b)
        self.assertIn('<a href="https://zh.wikipedia.org/wiki/multi">多范型</a>', merged)
        self.assertIn('<a href="https://zh.wikipedia.org/wiki/proc">过程式</a>', merged)
        self.assertIn("：", merged)
        self.assertIn("、", merged)
        # 两个主题段分开。
        lic = next(b for b in blocks if "许可证" in b and "软件基金会" in b)
        self.assertIn('<a href="https://zh.wikipedia.org/wiki/psf">Python软件基金会许可证</a>', lic)

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
        # 每个保留块都是完整闭合的。
        for b in kept:
            self.assertTrue(b.startswith("<p>") and b.endswith("</p>"))

    def test_no_truncation(self):
        blocks = ["<p>short</p>"]
        kept, truncated = _truncate_blocks(blocks, 100)
        self.assertEqual(kept, blocks)
        self.assertFalse(truncated)


class TestBuildResult(unittest.TestCase):
    def _make_media(self):
        return PageMedia(
            videos=[MediaAsset(url="https://cdn.example.com/v.mp4", label="宣传片")],
            embeds=[MediaAsset(
                url="https://www.youtube.com/watch?v=abc",
                label="Demo", source="embed", provider="YouTube",
            )],
            audios=[],
            images=[
                MediaAsset(url="https://cdn.example.com/a.jpg"),
                MediaAsset(url="https://cdn.example.com/b.jpg"),
            ],
        )

    def test_structure(self):
        media = self._make_media()
        result = build_fetch_rich_result(
            "https://example.com/post", "示例页面", ["<p>正文内容</p>"], media
        )
        self.assertIn("<h3>示例页面</h3>", result)
        self.assertIn('<p>🔗 <a href="https://example.com/post">example.com</a></p>', result)
        self.assertIn("<p>正文内容</p>", result)
        self.assertIn('<figure><video src="https://cdn.example.com/v.mp4"/><figcaption>宣传片</figcaption></figure>', result)
        self.assertIn('<a href="https://www.youtube.com/watch?v=abc">YouTube · Demo</a>', result)
        self.assertIn("<tg-slideshow>", result)

    def test_slideshow_contains_bare_imgs_only(self):
        media = self._make_media()
        result = build_fetch_rich_result("https://example.com/p", "t", [], media)
        slide = result[result.index("<tg-slideshow>"):result.index("</tg-slideshow>")]
        self.assertNotIn("<figure", slide)
        self.assertEqual(slide.count("<img"), 2)

    def test_inline_media_deduped_from_media_section(self):
        media = self._make_media()
        body = ['<img src="https://cdn.example.com/a.jpg"/>', "<p>文本</p>"]
        result = build_fetch_rich_result("https://example.com/p", "t", body, media)
        # a.jpg 已内联出现 → 幻灯片里只应剩下 b.jpg（单张 → 不用 slideshow）。
        self.assertNotIn("<tg-slideshow>", result)
        self.assertIn('<img src="https://cdn.example.com/b.jpg"/>', result)
        self.assertEqual(result.count('src="https://cdn.example.com/a.jpg"'), 1)

    def test_duplicate_heading_removed(self):
        media = PageMedia()
        result = build_fetch_rich_result(
            "https://example.com/p", "Same Title", ["<h1>Same Title</h1>", "<p>正文</p>"], media
        )
        self.assertNotIn("<h1>Same Title</h1>", result)
        self.assertIn("<h3>Same Title</h3>", result)

    def test_fallback_text_used_when_no_blocks(self):
        media = PageMedia()
        result = build_fetch_rich_result(
            "https://example.com/p", "标题", [], media, fallback_text="第一段。\n\n第二段。"
        )
        self.assertIn("<p>第一段。</p>", result)
        self.assertIn("<p>第二段。</p>", result)

    def test_long_content_truncated_at_block_boundary(self):
        media = PageMedia()
        blocks = [f"<p>{'内容' * 900}</p>" for _ in range(30)]
        result = build_fetch_rich_result("https://example.com/p", "标题", blocks, media)
        self.assertLessEqual(len(result), FETCH_RICH_MAX_LEN)
        self.assertIn("正文过长，已截断", result)
        # 截断后每个 <p> 都应当闭合。
        self.assertEqual(result.count("<p>"), result.count("</p>"))

    def test_single_image_no_slideshow(self):
        media = PageMedia(images=[MediaAsset(url="https://cdn.example.com/only.jpg")])
        result = build_fetch_rich_result("https://example.com/p", "t", ["<p>x</p>"], media)
        self.assertNotIn("<tg-slideshow>", result)
        self.assertIn('<img src="https://cdn.example.com/only.jpg"/>', result)


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

    def test_success_returns_telegram_html(self):
        se = self._patch_fetch(self.PAGE_HTML)
        result = run_async(se.execute_fetch_url("https://example.com/integration-test"))
        self.assertFalse(result.startswith("失败"))
        self.assertIn("<h3>集成测试页面</h3>", result)
        self.assertIn('<p>🔗 <a href="https://example.com/integration-test">example.com</a></p>', result)
        self.assertIn('<a href="https://example.com/more">更多阅读</a>', result)
        self.assertIn("<b>加粗</b>", result)
        self.assertIn('<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">YouTube</a>', result)
        # 正文里的 photo.jpg 由 trafilatura 内联提取为 figure；去重后媒体区
        # 只剩 og:cover.jpg 一张 → 单张不使用 slideshow，直接 <img>。
        self.assertIn('<figure><img src="https://cdn.example.com/photo.jpg"/><figcaption>新闻配图</figcaption></figure>', result)
        self.assertIn('<h4>🖼️ 图片</h4><img src="https://cdn.example.com/cover.jpg"/>', result)
        self.assertEqual(result.count('src="https://cdn.example.com/photo.jpg"'), 1)

    def test_failure_not_cached_and_prefix_preserved(self):
        se = self._patch_fetch(None, download=None)
        result = run_async(se.execute_fetch_url("https://example.com/empty-page"))
        self.assertTrue(result.startswith("失败："))
        # 失败结果不写入缓存。
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
