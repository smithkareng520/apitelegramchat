"""网关媒体拉取瞬态 400 的重试判定回归测试。

背景（2026-09-11 生产 [3a64f5cd]）：agnes-3.0-flash 等只接受 URL 输入的
网关，每次请求都要自行下载消息历史里的媒体 URL（R2 预签名地址）。R2
跨区域下载存在抖动——同一张图上一轮下载成功，下一轮即 400 +
"Timed out while downloading media URL"（upstream_error）。该错误形状
必须在首个增量前重放同一请求一次（与 ReadTimeout 首增量前重试同语义），
而不是把整轮报废成"请求失败"推给用户。

覆盖两个纯判定助手（重试分支内联在 _agentic_loop_openai_compat 的
except BadRequestError 里，语义即这两个函数的组合）：
- _looks_like_transient_media_fetch_error：错误形状匹配（含生产日志原文、
  大小写/包装变体；排除请求形状类 400 与宽泛 upstream_error）；
- _should_retry_media_fetch_400：重试闸门（首增量前 + 仅一次 + 形状匹配）。
"""
import httpx
import pytest
from openai import BadRequestError

from ai.agentic_loops import (
    _GATEWAY_MEDIA_FETCH_ERROR_MARKERS,
    _looks_like_transient_media_fetch_error,
    _should_retry_media_fetch_400,
)


def _bad_request(message: str) -> BadRequestError:
    """构造携带指定错误文本的真实 BadRequestError（与线上抛出形状一致）。"""
    response = httpx.Response(
        400, request=httpx.Request("POST", "https://gw.test/v1/chat/completions")
    )
    return BadRequestError(message, response=response, body=None)


# 生产日志 [3a64f5cd] 的原始错误文本（嵌套包装：openai -> LiteLLM ->
# OpenAIException -> JSON payload），打码不影响形状。
PRODUCTION_ERROR_TEXT = (
    "Error code: 400 - {'error': {'message': '***.BadRequestError: OpenAIException - "
    '{\\"object\\":\\"error\\",\\"message\\":\\"An exception occurred while loading IMAGE data '
    "at index 0: Error while loading data ImageData(url='https://***.com/***/***/*** "
    "Timed out while downloading media URL: https://***.com/***/***/***?X-Amz-Expires=***"
    "&X-Amz-SignedHeaders=***&X-Amz-Signature=***&X-Amz-Algorithm=***&X-Amz-Credential=***"
    "&X-Amz-Date=***', 'type': 'upstream_error', 'param': '', 'code': '400'}}"
)


class TestLooksLikeTransientMediaFetchError:
    def test_production_log_text_matches(self):
        assert _looks_like_transient_media_fetch_error(_bad_request(PRODUCTION_ERROR_TEXT))

    def test_minimal_timeout_marker_matches(self):
        assert _looks_like_transient_media_fetch_error(
            _bad_request("Timed out while downloading media URL: https://x")
        )

    def test_case_insensitive_wrapper_variants_match(self):
        # LiteLLM/网关多层包装后大小写漂移，子串匹配必须大小写不敏感
        assert _looks_like_transient_media_fetch_error(
            _bad_request("WRAPPER: TIMED OUT WHILE DOWNLOADING MEDIA URL: https://x")
        )
        assert _looks_like_transient_media_fetch_error(
            _bad_request("wrapped: AN EXCEPTION OCCURRED WHILE LOADING IMAGE data at index 0")
        )

    @pytest.mark.parametrize("kind", ["IMAGE", "VIDEO", "AUDIO", "FILE"])
    def test_all_media_kinds_match(self, kind):
        assert _looks_like_transient_media_fetch_error(
            _bad_request(f"An exception occurred while loading {kind} data at index 0")
        )

    def test_plain_upstream_error_alone_does_not_match(self):
        # 宽泛 upstream_error 不匹配：避免把非媒体类上游错误误判为可重试
        assert not _looks_like_transient_media_fetch_error(
            _bad_request("Bad Request: upstream_error code 400")
        )

    def test_request_shape_400_does_not_match(self):
        # 请求形状类 400（需要模型自纠 / 明确报给用户）绝不能吞掉重试
        assert not _looks_like_transient_media_fetch_error(
            _bad_request("is an image model. Use /v1/images/generations")
        )
        assert not _looks_like_transient_media_fetch_error(
            _bad_request("Invalid parameter: 'messages' must not be empty")
        )

    def test_strict_schema_rejection_does_not_match(self):
        assert not _looks_like_transient_media_fetch_error(
            _bad_request("response_format is not supported with strict tools")
        )

    def test_timeout_marker_list_is_nonempty(self):
        assert _GATEWAY_MEDIA_FETCH_ERROR_MARKERS


class TestShouldRetryMediaFetch400:
    def test_retry_allowed_before_first_increment(self):
        exc = _bad_request(PRODUCTION_ERROR_TEXT)
        assert _should_retry_media_fetch_400(exc, received_any=False, stream_attempt=0)

    def test_no_retry_after_any_increment(self):
        # 已消费增量后重放会产生半个模型回合，必须直接抛出
        exc = _bad_request(PRODUCTION_ERROR_TEXT)
        assert not _should_retry_media_fetch_400(exc, received_any=True, stream_attempt=0)

    def test_retry_only_once(self):
        # 第二次尝试（stream_attempt=1）失败必须放行给上层，不能无限重试
        exc = _bad_request(PRODUCTION_ERROR_TEXT)
        assert not _should_retry_media_fetch_400(exc, received_any=False, stream_attempt=1)

    def test_no_retry_for_non_media_400_even_fresh(self):
        exc = _bad_request("Invalid parameter: 'messages' must not be empty")
        assert not _should_retry_media_fetch_400(exc, received_any=False, stream_attempt=0)

    def test_plain_exception_types_work_too(self):
        # 判定只依赖 str(exc)，与异常具体类型解耦（网关可能抛自定义子类）
        assert _looks_like_transient_media_fetch_error(RuntimeError(PRODUCTION_ERROR_TEXT))
        assert not _looks_like_transient_media_fetch_error(ValueError("unrelated"))
