# =====================================================================
# tests/conftest.py — pytest 全局配置
# =====================================================================
# 把项目 src/ 目录加入 sys.path，使测试可以直接导入扁平化后的顶层模块
# （app.py / config.py / markdown_converter.py / ai/ / mcpserver/ 等）。
# 在导入任何项目模块之前执行，保证所有测试共享同一个导入根。
# =====================================================================
from pathlib import Path

import os
import sys
import tempfile

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---- 在导入任何项目模块之前，把工作空间根指到独立临时目录 ----
# workspace_paths.workspaces_root() 默认 /home 且带 lru_cache：不隔离的话，
# 未显式设置环境的测试会在真实 /home 下创建目录。这里给一个会话级临时
# 目录兑底；需要精确路径断言的测试自行 setenv 并清缓存。
# （本变量全进程只此一处设置；tests/integration/conftest.py 不再重复
# setdefault——目录字母序决定了那里的 setdefault 永不生效，只会留下
# "看似双重隔离、实为静默冲突"的陷阱。）
os.environ.setdefault(
    "APITELEGRAMCHAT_WORKSPACES_DIR",
    str(Path(tempfile.mkdtemp(prefix="apitelegramchat_test_ws_"))),
)

# ---- 注册测试夹具用的媒体模型（延迟到首个测试启动时执行） ----
# 生产 config.py 里 agnes-video-2.5 / google/gemini-3-pro-image-preview 的
# 注册项已注释（模型下线/配置驱动注册试点），但媒体管线单元测试仍以它们
# 为固定夹具。这里按 config.py 注释块的原始形状注册，仅测试进程生效；
# "不在才注册"的守卫保证与未来生产配置的恢复不冲突。
#
# ⚠️ 必须延迟注册：不能在 conftest 导入期 import config。pytest 按目录
# 字母序加载 conftest，根 conftest 先于 tests/integration/conftest.py 执行；
# 若在此处提前导入 config，WEBHOOK_TOKEN / INGEST_MODE 等环境变量尚未由
# integration conftest 设置，config 会以空值冻结，集成测试的 webhook
# 鉴权全部 403。session 级 autouse fixture 保证注册发生在"全部 collection
# 完成、首个测试开始前"——此时各目录 conftest 的环境变量都已就位。
import pytest


def _register_test_media_models() -> None:
    import config

    make = config.make_model_config
    if "agnes-video-2.5" not in config.SUPPORTED_MODELS:
        config.SUPPORTED_MODELS["agnes-video-2.5"] = make(
            model_id="agnes-video-2.5",
            provider="agnes",
            image_input=True,
            video_input=True,
            video_output=True,
            max_context=32768,
            max_output_tokens=4000,
            # 视频任务提交端点（配置驱动）：_request_agnes_video 优先 POST
            # 到该 URL，未声明时回退内置默认。
            endpoint="https://apihub.agnes-ai.com/v1/videos",
        )
    if "google/gemini-3-pro-image-preview" not in config.SUPPORTED_MODELS:
        config.SUPPORTED_MODELS["google/gemini-3-pro-image-preview"] = make(
            model_id="google/gemini-3-pro-image-preview",
            provider="openrouter",
            image_output=True,
            image_input=True,
            supports_tools=False,
            max_context=66000,
        )
    # agnes-image-2.1-flash：生产注册已下线，但 config.py 中 2.5 的注册注释
    # 明确说"与 2.1 同形状"，test_agnes_image_21_shares_same_shape 以它为
    # 路由回归夹具。形状与 2.5 完全一致（images 协议 + 完整图像端点）。
    if "agnes-image-2.1-flash" not in config.SUPPORTED_MODELS:
        config.SUPPORTED_MODELS["agnes-image-2.1-flash"] = make(
            model_id="agnes-image-2.1-flash",
            provider="agnes",
            image_output=True,
            image_input=True,
            supports_tools=False,
            max_context=4000,
            max_output_tokens=1024,
            protocol="openai_images",
            endpoint="https://apihub.agnes-ai.com/v1/images/generations",
        )


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_media_models():
    _register_test_media_models()
    yield


# ---- tiktoken 离线兜底 ----
# token_budget 用 tiktoken 的 o200k_base，首次使用需联网下载编码表；CI /
# 沙箱等无外网环境下载会失败（403/超时），令所有间接计数 token 的测试
# 连带失败。编码表本地可得时不做任何替换；不可得时退化为按字符计数的
# 确定性假编码——预算/截断类断言只依赖「计数单调、encode/decode 互逆」。
# 少数断言按真实分词粒度调校（如「恰好 ≤ N token」的边界），假编码下无意义，
# 离线兜底生效时自动跳过（见 _TOKENIZER_SENSITIVE_TESTS）。
class _CharEncoding:
    def encode(self, text, **_kw):
        return [ord(c) for c in text]

    def decode(self, tokens):
        return "".join(chr(t) for t in tokens)


_TOKENIZER_SENSITIVE_TESTS = {
    "test_1_long_reasoning_rolls_complete_at_reasoning_end",
    "test_truncate_blocks_squeezes_oversized_first_block_instead_of_dropping",
    "test_head_tail_keeps_both_ends_and_budget",
    "test_head_tail_head_is_prefix_tail_is_suffix",
}
_offline_cache: list = []


def _tiktoken_offline() -> bool:
    if not _offline_cache:
        import token_budget

        try:
            token_budget._get_encoding(token_budget.DEFAULT_ENCODING_NAME)
            _offline_cache.append(False)
        except Exception:
            token_budget._get_encoding.cache_clear()
            _offline_cache.append(True)
    return _offline_cache[0]


def pytest_collection_modifyitems(config, items):
    if not _tiktoken_offline():
        return
    skip = pytest.mark.skip(reason="需要真实 tiktoken 编码表（当前环境离线，使用字符计数兜底）")
    for item in items:
        if item.name in _TOKENIZER_SENSITIVE_TESTS:
            item.add_marker(skip)


@pytest.fixture(scope="session", autouse=True)
def _offline_tiktoken_fallback():
    if not _tiktoken_offline():
        yield
        return
    import token_budget

    import functools

    original = token_budget._get_encoding
    # 保持 lru_cache 形状：个别测试会调用 _get_encoding.cache_clear()。
    token_budget._get_encoding = functools.lru_cache(maxsize=4)(lambda _name: _CharEncoding())
    try:
        yield
    finally:
        token_budget._get_encoding = original
