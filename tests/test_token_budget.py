from __future__ import annotations

import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from apitelegramchat.context_manager import select_request_context
from apitelegramchat.ai.rich_message_builder import RichMessageBuilder, _scan_rich_html_boundaries
from apitelegramchat.fetch_rich_content import (
    FETCH_RESPONSE_TOKEN_BUDGET,
    _restore_severely_truncated_dom_tables,
    build_model_facing_html,
    extract_body_blocks,
)
from apitelegramchat.token_budget import count_tokens, truncate_to_token_budget
from apitelegramchat.tool_executors import TOOL_RESPONSE_TOKEN_BUDGET, _truncate_tool_result


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "apitelegramchat"


class TokenBudgetTests(unittest.TestCase):
    def test_truncation_is_exact_and_unicode_safe(self) -> None:
        text = "中文内容与 English content 混合。" * 400
        budget = 75
        truncated = truncate_to_token_budget(text, budget, suffix="…")
        self.assertLessEqual(count_tokens(truncated), budget)
        self.assertTrue(truncated)
        self.assertNotIn("\ufffd", truncated)

    def test_context_selection_uses_token_budget(self) -> None:
        history = [
            {"role": "user", "content": "早期消息 " * 300},
            {"role": "assistant", "content": "近期消息 " * 80},
        ]
        token_budget = count_tokens(history[-1].__repr__()) + 40
        snapshot = select_request_context(history, max_messages=10, max_tokens=token_budget)
        self.assertLessEqual(snapshot.estimated_tokens, token_budget)
        self.assertEqual(snapshot.messages[-1]["content"], history[-1]["content"])

    def test_global_tool_and_fetch_budgets_are_20000_tokens(self) -> None:
        self.assertEqual(TOOL_RESPONSE_TOKEN_BUDGET, 20_000)
        self.assertEqual(FETCH_RESPONSE_TOKEN_BUDGET, 20_000)
        oversized = "token budget validation " * 30_000
        result = _truncate_tool_result(oversized)
        self.assertLessEqual(count_tokens(result), TOOL_RESPONSE_TOKEN_BUDGET)

        fetch_result = build_model_facing_html(
            "https://example.com/article",
            "<html><head><title>示例文章</title></head><body><p>正文</p></body></html>",
            body_blocks=[f"<p>{'中文 English 内容 ' * 20_000}</p>"],
            title="示例文章",
        )
        self.assertIsNotNone(fetch_result)
        self.assertLessEqual(count_tokens(fetch_result), FETCH_RESPONSE_TOKEN_BUDGET)

    @staticmethod
    def _rich_table(rows: list[list[str]]) -> str:
        return "<table bordered striped>" + "".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
            for row in rows
        ) + "</table>"

    @staticmethod
    def _raw_episode_table(rows: list[list[str]], extra: str = "") -> str:
        rendered_rows = []
        for row_index, row in enumerate(rows):
            tag = "th" if row_index == 0 else "td"
            rendered_rows.append("<tr>" + "".join(f"<{tag}>{cell}</{tag}>" for cell in row) + "</tr>")
        return "<html><body><table class='wikitable'>" + "".join(rendered_rows) + extra + "</table></body></html>"

    def test_dom_table_fallback_restores_only_verified_missing_rows(self) -> None:
        raw_rows = [
            ["话数", "日文标题", "中文标题"],
            ["#01", "first", "第一集"],
            ["#02", "second", "第二集"],
            ["#03", "third", "第三集"],
            ["#04", "fourth", "第四集"],
        ]
        converted = [self._rich_table(raw_rows[:2])]
        restored = _restore_severely_truncated_dom_tables(converted, self._raw_episode_table(raw_rows))
        self.assertEqual(len(restored), 1)
        self.assertIn("#04", restored[0])
        self.assertIn("第四集", restored[0])
        self.assertNotIn("<script", restored[0].lower())

    def test_extract_body_blocks_applies_verified_table_fallback(self) -> None:
        raw_rows = [
            ["话数", "日文标题", "中文标题"],
            ["#01", "first", "第一集"],
            ["#02", "second", "第二集"],
            ["#03", "third", "第三集"],
            ["#04", "fourth", "第四集"],
        ]
        truncated_xml = """
            <doc><table>
              <row><cell role='head'>话数</cell><cell role='head'>日文标题</cell><cell role='head'>中文标题</cell></row>
              <row><cell>#01</cell><cell>first</cell><cell>第一集</cell></row>
            </table></doc>
        """
        fake_trafilatura = types.SimpleNamespace(extract=lambda *_args, **_kwargs: truncated_xml)
        with patch.dict(sys.modules, {"trafilatura": fake_trafilatura}):
            blocks = extract_body_blocks(self._raw_episode_table(raw_rows), "https://example.test")
        rendered = "\n".join(blocks)
        self.assertIn("#04", rendered)
        self.assertIn("第四集", rendered)

    def test_dom_table_fallback_preserves_complete_or_unmatched_table(self) -> None:
        raw_rows = [
            ["话数", "日文标题", "中文标题"],
            ["#01", "first", "第一集"],
            ["#02", "second", "第二集"],
            ["#03", "third", "第三集"],
            ["#04", "fourth", "第四集"],
        ]
        complete = [self._rich_table(raw_rows)]
        self.assertEqual(
            _restore_severely_truncated_dom_tables(complete, self._raw_episode_table(raw_rows)),
            complete,
        )
        unmatched = [self._rich_table([["话数", "日文标题", "中文标题"], ["#99", "other", "其他"]])]
        self.assertEqual(
            _restore_severely_truncated_dom_tables(unmatched, self._raw_episode_table(raw_rows)),
            unmatched,
        )

    def test_dom_table_fallback_escapes_untrusted_source_html(self) -> None:
        raw_rows = [
            ["话数", "日文标题", "中文标题"],
            ["#01", "first", "第一集"],
            ["#02", "<b>second</b>", "第二集"],
            ["#03", "<script>bad()</script>third", "第三集"],
            ["#04", "<img src=x onerror=bad()>fourth", "第四集"],
        ]
        raw_html = self._raw_episode_table(raw_rows)
        converted = [self._rich_table(raw_rows[:2])]
        restored = _restore_severely_truncated_dom_tables(converted, raw_html)[0]
        self.assertIn("second", restored)
        self.assertIn("third", restored)
        self.assertIn("fourth", restored)
        self.assertNotIn("<script", restored.lower())
        self.assertNotIn("onerror", restored.lower())
        self.assertNotIn("<img", restored.lower())

    def test_rich_boundary_scan_returns_token_metadata(self) -> None:
        boundaries, visible_tokens, block_count, visible_units = _scan_rich_html_boundaries(
            "<p>中文 English</p><p>下一段</p>"
        )
        self.assertEqual(len(boundaries[-1]), 4)
        self.assertGreater(visible_tokens, 0)
        self.assertEqual(block_count, 2)
        self.assertGreater(visible_units, 0)

    def test_rich_builder_flush_accepts_four_value_scan_result(self) -> None:
        async def run_flush() -> None:
            builder = RichMessageBuilder(chat_id=1)
            builder.blocks = ["<p>flush regression check</p>"]
            builder.block_types = ["html"]
            with patch(
                "apitelegramchat.ai.rich_message_builder.send_rich_message_draft",
                new=AsyncMock(return_value=None),
            ) as send_draft:
                await builder.flush(force=True)
                send_draft.assert_awaited_once()

        import asyncio
        asyncio.run(run_flush())

    def test_no_legacy_length_identifiers_remain(self) -> None:
        legacy = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:LEN|CHARS)\b")
        findings: list[str] = []
        for path in SOURCE_ROOT.rglob("*.py"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if legacy.search(line):
                    findings.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}: {line}")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
