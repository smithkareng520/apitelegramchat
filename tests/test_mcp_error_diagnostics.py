"""外部 MCP 失败分类与用户提示的回归测试。"""

import unittest

from apitelegramchat.mcp_client import (
    MCPToolError,
    _classify_failure,
    _diagnose_mcp_exception,
    _truncate_safe_detail,
)


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeHttpError(RuntimeError):
    def __init__(self, status_code: int, text: str) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = _FakeResponse(status_code, text)


class MCPErrorDiagnosticsTests(unittest.TestCase):
    def test_gateway_error_keeps_502_and_remains_retryable(self) -> None:
        status, category, detail, retryable = _diagnose_mcp_exception(
            _FakeHttpError(502, "bad gateway")
        )
        self.assertEqual(status, 502)
        self.assertEqual(category, "gateway")
        self.assertTrue(retryable)
        self.assertIn("bad gateway", detail)

    def test_rate_limit_keeps_429_and_is_not_retryable(self) -> None:
        status, category, _, retryable = _diagnose_mcp_exception(
            _FakeHttpError(429, '{"error":{"message":"Request limit exceeded."}}')
        )
        self.assertEqual(status, 429)
        self.assertEqual(category, "rate_limited")
        self.assertFalse(retryable)

    def test_text_only_quota_error_is_classified(self) -> None:
        category, retryable = _classify_failure(None, "quota exhausted")
        self.assertEqual(category, "rate_limited")
        self.assertFalse(retryable)

    def test_exception_group_is_recursively_unwrapped(self) -> None:
        grouped = ExceptionGroup(
            "task group failure",
            [_FakeHttpError(404, "Not Found")],
        )
        status, category, detail, retryable = _diagnose_mcp_exception(grouped)
        self.assertEqual(status, 404)
        self.assertEqual(category, "endpoint")
        self.assertFalse(retryable)
        self.assertIn("Not Found", detail)

    def test_observed_status_recovers_sdk_swallowed_404(self) -> None:
        status, category, _, retryable = _diagnose_mcp_exception(
            RuntimeError("Session terminated"),
            observed_status_code=404,
        )
        self.assertEqual(status, 404)
        self.assertEqual(category, "endpoint")
        self.assertFalse(retryable)

    def test_sensitive_values_are_redacted_from_diagnostics(self) -> None:
        detail = _truncate_safe_detail(
            "Authorization: Bearer secret-token api_key=also-secret"
        )
        self.assertNotIn("secret-token", detail)
        self.assertNotIn("also-secret", detail)
        self.assertIn("***", detail)

    def test_user_message_distinguishes_gateway_from_quota(self) -> None:
        gateway = MCPToolError("failed", category="gateway", status_code=502)
        quota = MCPToolError(
            "limited", category="rate_limited", status_code=429, retryable=False
        )
        endpoint = MCPToolError(
            "missing", category="endpoint", status_code=404, retryable=False
        )
        self.assertIn("不能证明调用额度已用完", gateway.user_message("网页搜索服务"))
        self.assertIn("限流或调用额度限制", quota.user_message("网页搜索服务"))
        self.assertIn("部署地址不存在", endpoint.user_message("网页搜索服务"))


if __name__ == "__main__":
    unittest.main()
