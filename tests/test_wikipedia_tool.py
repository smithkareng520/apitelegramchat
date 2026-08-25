# tests/test_wikipedia_tool.py — wikipedia 工具富 HTML 升级测试
#
# 运行：PYTHONPATH=src python -m pytest tests/test_wikipedia_tool.py -v
# 全部离线（mock MediaWiki API），覆盖：
#   1. 主路径：list=search → action=parse 完整 HTML → 富管线输出
#      （与 fetch_url 同格式的 Telegram Rich HTML，表格/标题/链接保留）
#   2. 退化路径：parse 失败（HTTP 500 / 空 HTML）→ 纯文本摘要（历史格式）
#   3. 语言回退：zh 无结果 → en 命中
#   4. 彻底失败：两种语言都无结果 → "失败：" 前缀
#   5. 下游消费方：format_tool_result（UI 标题 + 真实 URL）与
#      _generate_tool_summary_done（Looked up: 标题 / 失败识别）
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def run_async(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Mock 基础设施：模拟 curl_cffi AsyncSession 打 MediaWiki API
# ---------------------------------------------------------------------------

class _FakeWikiResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeWikiApi:
    """按 (语言, 请求类型) 分发预设响应。

    请求类型：search（list=search）/ parse（action=parse）/ extract
    （prop=extracts 纯文本）。未配置的组合返回 404。
    """

    def __init__(self, responses):
        # responses: {(lang, kind): _FakeWikiResp}
        self.responses = responses

    def __call__(self):
        api = self

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, params=None, **kwargs):
                lang = url.split("//", 1)[1].split(".", 1)[0]
                action = (params or {}).get("action")
                if action == "query" and "list" in (params or {}):
                    kind = "search"
                elif action == "parse":
                    kind = "parse"
                else:
                    kind = "extract"
                return api.responses.get((lang, kind), _FakeWikiResp(status_code=404))

        return _Session()


# MediaWiki action=parse 的真实输出形态（片段）：标题 h2 内嵌
# span.mw-headline；表格为 table.wikitable。
WIKI_PARSE_HTML = (
    '<div class="mw-parser-output">'
    "<p><b>可塑性記憶</b>（Plastic Memories）是2015年4月至6月播出的日本电视动画作品，"
    "由动画工房制作，全13话。本段文字较长是为了让 trafilatura 稳定提取正文。</p>"
    '<h2 id="故事簡介"><span class="mw-headline" id="故事簡介">故事簡介</span></h2>'
    "<p>故事发生在机器人技术高度发达的近未来世界，主角们负责回收寿命将尽的仿生机器人"
    "Giftia，这是一份既温柔又残酷的工作，段落内容足够长以供提取测试使用。</p>"
    '<h2 id="各話列表"><span class="mw-headline" id="各話列表">各話列表</span></h2>'
    '<table class="wikitable"><tbody>'
    "<tr><th>話數</th><th>日文標題</th><th>中文標題</th></tr>"
    "<tr><td>第1話</td><td>はじめてのパートナー</td><td>初次見面的搭檔</td></tr>"
    "<tr><td>第2話</td><td>足を引っ張りたくないので</td><td>因為不想扯後腿</td></tr>"
    "<tr><td>第13話</td><td>いつかまた巡り会えますように</td><td>但願有天能夠再次相逢</td></tr>"
    "</tbody></table>"
    '<p><a rel="nofollow" class="external text" href="https://www.plastic-memories.jp/">官方网站</a></p>'
    "</div>"
)

_SEARCH_OK = {"query": {"search": [{"pageid": 4508998, "title": "可塑性記憶"}]}}
_PARSE_OK = {"parse": {"title": "可塑性記憶", "text": {"*": WIKI_PARSE_HTML}}}
_EXTRACT_OK = {
    "query": {
        "pages": {
            "4508998": {
                "title": "可塑性記憶",
                "extract": "可塑性記憶是2015年的日本电视动画，全13话。",
                "fullurl": "https://zh.wikipedia.org/wiki/%E5%8F%AF%E5%A1%91%E6%80%A7%E8%A8%98%E6%86%B6",
            }
        }
    }
}
_SEARCH_EMPTY = {"query": {"search": []}}


def _patch_wiki_api(responses):
    import apitelegramchat.search_engine as se

    original = se.AsyncSession
    se.AsyncSession = _FakeWikiApi(responses)

    def restore():
        se.AsyncSession = original

    return restore


class TestExecuteWikipediaRich(unittest.TestCase):
    """主路径：action=parse 完整 HTML → 富管线。"""

    def setUp(self):
        self._restore = _patch_wiki_api(
            {
                ("zh", "search"): _FakeWikiResp(payload=_SEARCH_OK),
                ("zh", "parse"): _FakeWikiResp(payload=_PARSE_OK),
                ("zh", "extract"): _FakeWikiResp(payload=_EXTRACT_OK),
            }
        )
        self.addCleanup(self._restore)
        import apitelegramchat.search_engine as se
        self.se = se

    def test_returns_rich_html_with_tables(self):
        result = run_async(self.se.execute_wikipedia("可塑性记忆", lang="zh"))
        self.assertFalse(result.startswith("失败"))
        # 与 fetch_url 相同的页头格式。
        self.assertIn("<h3>可塑性記憶</h3>", result)
        self.assertIn('<p>🔗 <a href="https://zh.wikipedia.org/wiki/', result)
        # 表格（各话列表）完整保留——纯文本摘要永远做不到。
        self.assertIn("第1話", result)
        self.assertIn("はじめてのパートナー", result)
        self.assertIn("第13話", result)
        self.assertIn("いつかまた巡り会えますように", result)
        self.assertIn("<table", result)
        # 说明：合成片段太小，trafilatura 可能丢弃 h2 结构标题与
        # "仅含外链"的末段（真实页面集成测试已验证两者保留），
        # 此处只断言内容级关键词。
        self.assertIn("故事发生在", result)
        # 主路径不应是纯文本退化格式。
        self.assertNotIn("<b>Wikipedia —", result)

    def test_parse_params_disable_noise(self):
        """action=parse 请求应禁用编辑链接/目录/限制报告等噪音。"""
        import apitelegramchat.search_engine as se

        captured = {}

        class _CapturingApi(_FakeWikiApi):
            def __call__(self):
                api = self

                class _Session:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *exc):
                        return False

                    async def get(self, url, params=None, **kwargs):
                        if (params or {}).get("action") == "parse":
                            captured.update(params)
                        return await _FakeWikiApi.__call__(api).get(url, params, **kwargs)

                return _Session()

        original = se.AsyncSession
        se.AsyncSession = _CapturingApi(
            {
                ("zh", "search"): _FakeWikiResp(payload=_SEARCH_OK),
                ("zh", "parse"): _FakeWikiResp(payload=_PARSE_OK),
            }
        )
        self.addCleanup(lambda: setattr(se, "AsyncSession", original))
        run_async(se.execute_wikipedia("可塑性记忆", lang="zh"))
        self.assertEqual(captured.get("action"), "parse")
        self.assertEqual(captured.get("disableeditsection"), 1)
        self.assertEqual(captured.get("disabletoc"), 1)
        self.assertEqual(captured.get("redirects"), 1)


class TestExecuteWikipediaFallback(unittest.TestCase):
    """退化路径：parse 不可用时回退纯文本摘要（历史格式）。"""

    def _run(self, responses):
        restore = _patch_wiki_api(responses)
        self.addCleanup(restore)
        import apitelegramchat.search_engine as se
        return run_async(se.execute_wikipedia("可塑性记忆", lang="zh"))

    def test_parse_http_error_falls_back_to_extract(self):
        result = self._run(
            {
                ("zh", "search"): _FakeWikiResp(payload=_SEARCH_OK),
                ("zh", "parse"): _FakeWikiResp(status_code=500),
                ("zh", "extract"): _FakeWikiResp(payload=_EXTRACT_OK),
            }
        )
        self.assertFalse(result.startswith("失败"))
        self.assertIn("<b>Wikipedia — 可塑性記憶</b>", result)
        self.assertIn("全13话", result)
        self.assertIn("https://zh.wikipedia.org/wiki/", result)

    def test_parse_empty_html_falls_back_to_extract(self):
        result = self._run(
            {
                ("zh", "search"): _FakeWikiResp(payload=_SEARCH_OK),
                ("zh", "parse"): _FakeWikiResp(payload={"parse": {"title": "可塑性記憶", "text": {"*": ""}}}),
                ("zh", "extract"): _FakeWikiResp(payload=_EXTRACT_OK),
            }
        )
        self.assertIn("<b>Wikipedia — 可塑性記憶</b>", result)

    def test_parse_exception_falls_back_to_extract(self):
        result = self._run(
            {
                ("zh", "search"): _FakeWikiResp(payload=_SEARCH_OK),
                ("zh", "parse"): _FakeWikiResp(payload={"error": "boom"}),
                ("zh", "extract"): _FakeWikiResp(payload=_EXTRACT_OK),
            }
        )
        # parse 返回 JSON 无 parse 键 → page_html 为空 → 走退化路径。
        self.assertIn("<b>Wikipedia — 可塑性記憶</b>", result)


class TestExecuteWikipediaLanguageFallback(unittest.TestCase):
    def test_zh_empty_falls_back_to_en(self):
        restore = _patch_wiki_api(
            {
                ("zh", "search"): _FakeWikiResp(payload=_SEARCH_EMPTY),
                ("en", "search"): _FakeWikiResp(payload={"query": {"search": [{"pageid": 999, "title": "Plastic Memories"}]}}),
                ("en", "parse"): _FakeWikiResp(payload={"parse": {"title": "Plastic Memories", "text": {"*": WIKI_PARSE_HTML}}}),
                ("en", "extract"): _FakeWikiResp(payload={"query": {"pages": {"999": {"title": "Plastic Memories", "extract": "Japanese anime.", "fullurl": "https://en.wikipedia.org/wiki/Plastic_Memories"}}}}),
            }
        )
        self.addCleanup(restore)
        import apitelegramchat.search_engine as se
        result = run_async(se.execute_wikipedia("可塑性记忆", lang="zh"))
        self.assertFalse(result.startswith("失败"))
        # 命中的是 en 页面。
        self.assertIn("en.wikipedia.org", result)

    def test_all_failures_return_failure_prefix(self):
        restore = _patch_wiki_api(
            {
                ("zh", "search"): _FakeWikiResp(payload=_SEARCH_EMPTY),
                ("en", "search"): _FakeWikiResp(payload=_SEARCH_EMPTY),
            }
        )
        self.addCleanup(restore)
        import apitelegramchat.search_engine as se
        result = run_async(se.execute_wikipedia("不存在的词条xyz", lang="zh"))
        self.assertTrue(result.startswith("失败："))
        self.assertIn("Wikipedia", result)


class TestWikipediaConsumers(unittest.TestCase):
    """下游消费方：UI 展示（format_tool_result）与完成摘要（tool_summary）。"""

    RICH_RESULT = (
        "<h3>可塑性記憶</h3>\n"
        '<p>🔗 <a href="https://zh.wikipedia.org/wiki/%E5%8F%AF%E5%A1%91%E6%80%A7%E8%A8%98%E6%86%B6">zh.wikipedia.org</a></p>\n'
        "<p>可塑性記憶是2015年的日本电视动画。</p>\n"
        '<table bordered striped><tr><td><b>話數</b></td></tr><tr><td>第1話</td></tr></table>'
    )
    LEGACY_RESULT = (
        "<b>Wikipedia — 可塑性記憶</b><br/><br/>可塑性記憶是2015年的日本电视动画，全13话。"
        "<br/><br/>链接：https://zh.wikipedia.org/wiki/%E5%8F%AF%E5%A1%91%E6%80%A7%E8%A8%98%E6%86%B6"
    )

    def test_ui_shows_resolved_title_and_real_url(self):
        from apitelegramchat.tool_executors import format_tool_result

        summary, details = run_async(
            format_tool_result("wikipedia", {"query": "可塑性记忆", "lang": "zh"}, self.RICH_RESULT)
        )
        # 标题来自 <h3>（解析后的页面标题），而非原始 query。
        self.assertEqual(summary, "📚 可塑性記憶")
        # 链接用结果里的真实 URL（query 猜测的 /wiki/可塑性记忆 会 404）。
        self.assertIn(
            'https://zh.wikipedia.org/wiki/%E5%8F%AF%E5%A1%91%E6%80%A7%E8%A8%98%E6%86%B6', details
        )
        self.assertIn("可塑性記憶", details)
        # 富 HTML 正文不出现在 UI 折叠面板里。
        self.assertNotIn("<table", details)

    def test_ui_legacy_format_compat(self):
        from apitelegramchat.tool_executors import format_tool_result

        summary, details = run_async(
            format_tool_result("wikipedia", {"query": "可塑性记忆", "lang": "zh"}, self.LEGACY_RESULT)
        )
        self.assertEqual(summary, "📚 可塑性記憶")
        self.assertIn("%E5%8F%AF%E5%A1%91%E6%80%A7%E8%A8%98%E6%86%B6", details)

    def test_done_summary_parses_title(self):
        from apitelegramchat.ai.tool_summary import _generate_tool_summary_done

        self.assertEqual(
            _generate_tool_summary_done("wikipedia", {"query": "可塑性记忆"}, self.RICH_RESULT),
            "Looked up: 可塑性記憶",
        )
        self.assertEqual(
            _generate_tool_summary_done("wikipedia", {"query": "可塑性记忆"}, self.LEGACY_RESULT),
            "Looked up: 可塑性記憶",
        )

    def test_done_summary_failure(self):
        from apitelegramchat.ai.tool_summary import _generate_tool_summary_done

        summary = _generate_tool_summary_done(
            "wikipedia", {"query": "不存在"}, "失败：Wikipedia 查询「不存在」未找到结果。"
        )
        self.assertTrue(summary.startswith("Failed to look up"))

    def test_failure_detection(self):
        from apitelegramchat.ai.tool_summary import _tool_result_is_failure

        self.assertFalse(_tool_result_is_failure("wikipedia", {}, self.RICH_RESULT))
        self.assertTrue(_tool_result_is_failure("wikipedia", {}, "失败：Wikipedia 查询「x」未找到结果。"))
        # 正文含"失敗"字样不误判（百科条目可能谈论失败战役等）。
        self.assertFalse(
            _tool_result_is_failure("wikipedia", {}, "<h3>某战役</h3><p>这次军事行动失敗了……</p>")
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
