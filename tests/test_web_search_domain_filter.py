"""web_search 域名黑名单逐条规则的回归测试。"""

import unittest

from apitelegramchat import web_search_filter, web_search_settings


class WebSearchDomainFilterTests(unittest.TestCase):
    def test_global_match_mode_is_removed(self) -> None:
        self.assertFalse(hasattr(web_search_settings, "WEB_SEARCH_DOMAIN_MATCH_MODE"))

    def test_config_contains_requested_blacklist_domains(self) -> None:
        expected = {
            "zhihu.com", "weixin.qq.com", "xiaohongshu.com", "douyin.com",
            "kuaishou.com", "bilibili.com", "weibo.com", "csdn.net",
            "jb51.net", "360doc.com", "baijiahao.baidu.com", "zhidao.baidu.com",
        }
        self.assertEqual(set(web_search_filter.BLACKLISTED_SEARCH_DOMAINS), expected)

    def test_all_subdomain_rules_map_to_upstream_site_operators(self) -> None:
        terms = web_search_filter.upstream_domain_exclude_terms()
        self.assertEqual(
            terms.split(","),
            [
                "site:zhihu.com", "site:weixin.qq.com", "site:xiaohongshu.com",
                "site:douyin.com", "site:kuaishou.com", "site:bilibili.com",
                "site:weibo.com", "site:csdn.net", "site:jb51.net", "site:360doc.com",
            ],
        )

    def test_all_subdomain_rule_matches_root_and_descendants(self) -> None:
        self.assertTrue(web_search_filter.is_blacklisted_search_url("https://zhihu.com/question/1"))
        self.assertTrue(web_search_filter.is_blacklisted_search_url("https://www.zhihu.com/question/1"))
        self.assertTrue(web_search_filter.is_blacklisted_search_url("https://zhuanlan.zhihu.com/p/1"))

    def test_exact_rules_do_not_block_descendants(self) -> None:
        self.assertTrue(web_search_filter.is_blacklisted_search_url("https://baijiahao.baidu.com/article/1"))
        self.assertTrue(web_search_filter.is_blacklisted_search_url("https://zhidao.baidu.com/question/1"))
        self.assertFalse(web_search_filter.is_blacklisted_search_url("https://foo.baijiahao.baidu.com/article/1"))
        self.assertFalse(web_search_filter.is_blacklisted_search_url("https://foo.zhidao.baidu.com/question/1"))

    def test_rule_parser_supports_all_three_match_scopes(self) -> None:
        rules = {
            rule.raw: rule
            for rule in web_search_filter.parse_blacklist_rules(
                ["exact.example.com", "[*.]all.example.com", "*.sub.example.com"]
            )
        }
        self.assertTrue(rules["exact.example.com"].matches("exact.example.com"))
        self.assertFalse(rules["exact.example.com"].matches("a.exact.example.com"))

        self.assertTrue(rules["[*.]all.example.com"].matches("all.example.com"))
        self.assertTrue(rules["[*.]all.example.com"].matches("a.all.example.com"))
        self.assertTrue(rules["[*.]all.example.com"].matches("a.b.all.example.com"))

        self.assertFalse(rules["*.sub.example.com"].matches("sub.example.com"))
        self.assertTrue(rules["*.sub.example.com"].matches("a.sub.example.com"))
        self.assertTrue(rules["*.sub.example.com"].matches("a.b.sub.example.com"))

    def test_similar_but_unrelated_domain_is_not_blocked(self) -> None:
        self.assertFalse(web_search_filter.is_blacklisted_search_url("https://notzhihu.com/article"))
        self.assertFalse(web_search_filter.is_blacklisted_search_url("https://example.com/path?next=zhihu.com"))

    def test_filter_removes_only_blacklisted_results(self) -> None:
        items = [
            {"title": "知乎", "link": "https://www.zhihu.com/question/1", "snippet": "blocked"},
            {"title": "Example", "link": "https://example.com/article", "snippet": "kept"},
            {"title": "百家号子域名", "link": "https://foo.baijiahao.baidu.com/a", "snippet": "kept"},
        ]

        kept, filtered_count = web_search_filter.filter_blacklisted_search_results(items)

        self.assertEqual(filtered_count, 1)
        self.assertEqual(
            [item["link"] for item in kept],
            ["https://example.com/article", "https://foo.baijiahao.baidu.com/a"],
        )

    def test_candidate_count_respects_configuration_bounds(self) -> None:
        expected = min(
            max(10, 10 * web_search_filter.SEARCH_CANDIDATE_MULTIPLIER),
            web_search_filter.SEARCH_MAX_CANDIDATES,
        )
        self.assertEqual(web_search_filter.candidate_result_count(10), expected)
        self.assertLessEqual(
            web_search_filter.candidate_result_count(web_search_filter.SEARCH_MAX_RESULTS),
            web_search_filter.SEARCH_MAX_CANDIDATES,
        )

    def test_parser_ignores_invalid_rules(self) -> None:
        domains = web_search_filter.normalize_blacklist_domains(
            [" ZHIHU.COM.", "*.example.com", "[*.]all.example.com", "https://invalid.example", "bad/path", "a*b.example", 42]
        )
        self.assertEqual(domains, ("zhihu.com", "example.com", "all.example.com"))


if __name__ == "__main__":
    unittest.main()
