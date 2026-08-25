from __future__ import annotations

import re
import unittest
from pathlib import Path

from apitelegramchat.context_manager import select_request_context
from apitelegramchat.fetch_rich_content import FETCH_RESPONSE_TOKEN_BUDGET, build_model_facing_html
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
