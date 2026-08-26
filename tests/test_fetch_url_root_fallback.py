"""fetch_url 根路径首页回退的回归测试。"""

import unittest
from unittest.mock import patch

from apitelegramchat import fetch_url_fallback


class RootFallbackUrlTests(unittest.TestCase):
    def test_root_url_generates_same_origin_index_fallback(self) -> None:
        self.assertEqual(
            fetch_url_fallback.root_fallback_urls("https://www.battleofballs.com/"),
            ("https://www.battleofballs.com/index/",),
        )

    def test_root_url_preserves_scheme_host_and_port(self) -> None:
        self.assertEqual(
            fetch_url_fallback.root_fallback_urls("http://example.com:8080"),
            ("http://example.com:8080/index/",),
        )

    def test_only_clean_root_urls_can_fallback(self) -> None:
        for url in (
            "https://example.com/news/",
            "https://example.com/?lang=zh",
            "https://example.com/#hero",
            "ftp://example.com/",
            "https://example.com/index/",
        ):
            with self.subTest(url=url):
                self.assertEqual(fetch_url_fallback.root_fallback_urls(url), ())

    def test_invalid_configured_paths_are_ignored(self) -> None:
        with patch.object(
            fetch_url_fallback,
            "ROOT_FALLBACK_PATHS",
            fetch_url_fallback._configured_paths(
                ("/index/", "//evil.example/", "https://evil.example/", "/x?q=1", "/#part", "/")
            ),
        ):
            self.assertEqual(
                fetch_url_fallback.root_fallback_urls("https://example.com/"),
                ("https://example.com/index/",),
            )

    def test_disabled_feature_generates_no_candidates(self) -> None:
        with patch.object(fetch_url_fallback, "FETCH_URL_ROOT_FALLBACK_ENABLED", False):
            self.assertEqual(
                fetch_url_fallback.root_fallback_urls("https://example.com/"),
                (),
            )


if __name__ == "__main__":
    unittest.main()
