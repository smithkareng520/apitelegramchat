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
            name="Agnes video 2.5",
            vision=True,
            video=True,
            native_video=True,
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
            name="Gemini 3 Pro Image Preview",
            native_image=True,
            vision=True,
            supports_tools=False,
            max_context=66000,
        )
    # agnes-image-2.1-flash：生产注册已下线，但 config.py 中 2.5 的注册注释
    # 明确说"与 2.1 同形状"，test_agnes_image_21_shares_same_shape 以它为
    # 路由回归夹具。形状与 2.5 完全一致（images 协议 + 官方生成端点）。
    if "agnes-image-2.1-flash" not in config.SUPPORTED_MODELS:
        config.SUPPORTED_MODELS["agnes-image-2.1-flash"] = make(
            model_id="agnes-image-2.1-flash",
            provider="agnes",
            name="Agnes Image 2.1 Flash",
            native_image=True,
            vision=True,
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
