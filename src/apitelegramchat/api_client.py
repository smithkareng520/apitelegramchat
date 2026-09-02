# api_client.py
"""
统一 API 客户端工厂（配置驱动）
支持通过 config.py 中的 PROVIDERS 字典动态添加新厂商

安全改进：所有 API Key 从 config 模块的变量中读取（而非 os.environ），
配合 config.py 的 scrub_environment() 实现环境变量完全清洗。
"""

import logging
import os
import httpx
from typing import Dict, Optional, Union
from openai import AsyncOpenAI

try:
    # 可选依赖：仅 anthropic 厂商需要。未安装时其余厂商完全不受影响，
    # 只有实际请求 anthropic 客户端时才会报错（而不是在导入期整体失败）。
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - 依赖缺失时的降级路径
    AsyncAnthropic = None

# 从 config 导入厂商配置、ProviderConfig 以及所有 API Key 变量
from apitelegramchat.config import PROVIDERS
import apitelegramchat.config as app_config

logger = logging.getLogger(__name__)

# 使用原生 SDK（而非 AsyncOpenAI）的厂商集合。新增厂商只需加进这里 +
# config.py 的 PROVIDERS，_build_client 会自动分流，其余厂商的构造逻辑
# 不受影响。
_NATIVE_SDK_PROVIDERS = {"anthropic"}


class APIClient:
    """
    统一管理所有第三方 API 客户端。
    根据 api_type 动态创建并缓存客户端实例：
      - 默认厂商：AsyncOpenAI（OpenAI 兼容协议，逻辑与之前完全一致）
      - _NATIVE_SDK_PROVIDERS 中的厂商：对应的原生 SDK 客户端
        （目前仅 anthropic -> AsyncAnthropic）
    添加新的 OpenAI 兼容厂商只需在 config.py 的 PROVIDERS 中配置，无需修改本文件。
    """

    def __init__(self):
        self._clients: Dict[str, Union[AsyncOpenAI, "AsyncAnthropic"]] = {}
        self._providers = PROVIDERS  # 引用配置

    def _get_api_key(self, env_var: str) -> Optional[str]:
        """
        从 config 模块的变量中获取 API Key（而非从 os.environ 动态读取）。
        这样即使 scrub_environment() 清洗了 os.environ，应用仍能正常读取。
        """
        # 直接从 config 模块中读取对应变量（如 config.OPENROUTER_API_KEY）
        key = getattr(app_config, env_var, None)
        if not key:
            logger.warning(f"config 模块中 {env_var} 未设置或为空")
        return key

    def _build_native_client(self, provider: str, config) -> "AsyncAnthropic":
        """
        构建原生 SDK 客户端（目前仅 anthropic）。与 _build_client 的
        AsyncOpenAI 分支完全独立，互不影响。
        """
        if provider == "anthropic":
            if AsyncAnthropic is None:
                raise ValueError(
                    "未安装 anthropic 包，无法创建 Anthropic 客户端。"
                    "请在 requirements.txt / pyproject.toml 中确认 anthropic 依赖已安装。"
                )
            api_key = self._get_api_key(config.api_key_env)
            if not api_key:
                raise ValueError(f"缺少 API Key: {config.api_key_env}，请设置环境变量")
            logger.debug(f"创建 {config.name} 原生客户端")
            return AsyncAnthropic(
                api_key=api_key,
                # 与 OpenAI 兼容客户端保持相近的超时预算：连接短、流读取
                # 放宽到 300s（agentic 多轮工具调用后首个事件可能较晚）。
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=60.0),
            )
        raise ValueError(f"未知的原生 SDK 厂商: {provider}")

    def _build_client(self, provider: str) -> AsyncOpenAI:
        """
        根据厂商配置构建 AsyncOpenAI 客户端
        如果配置缺失或 API Key 为空，抛出 ValueError
        """
        config = self._providers.get(provider)
        if not config:
            raise ValueError(f"未知厂商: {provider}，请检查 config.py 中的 PROVIDERS")

        # anthropic 等原生 SDK 厂商走独立分支，完全不影响以下 AsyncOpenAI
        # 构造逻辑（现有厂商行为零变化）。
        if provider in _NATIVE_SDK_PROVIDERS:
            return self._build_native_client(provider, config)

        api_key = self._get_api_key(config.api_key_env)
        if not api_key:
            raise ValueError(f"缺少 API Key: {config.api_key_env}，请设置环境变量")

        # 默认 headers（如有）
        headers = config.default_headers or {}

        logger.debug(f"创建 {config.name} 客户端，base_url={config.base_url}")

        # 禁用 OpenAI SDK 的隐式自动重试。SDK 默认会依据服务端 Retry-After
        # 睡眠，可能出现“Retrying request ... in 60.000000 seconds”，并且会与
        # agentic_loops 的首增量超时重试叠加。429/5xx 由上层统一处理，避免一次
        # 请求在 SDK 内部无感等待数十秒后才返回。
        try:
            sdk_max_retries = max(0, int(os.getenv("OPENAI_SDK_MAX_RETRIES", "0")))
        except (TypeError, ValueError):
            sdk_max_retries = 0

        return AsyncOpenAI(
            base_url=config.base_url,
            api_key=api_key,
            max_retries=sdk_max_retries,
            # Agent 在多轮工具调用后，下一轮 SSE 的首个事件可能显著晚于普通聊天。
            # 使用分项超时：连接保持短，流读取允许 300 秒，避免 90 秒默认值中断长任务。
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=60.0),
            default_headers=headers,
        )

    def get_client(self, api_type: str) -> Union[AsyncOpenAI, "AsyncAnthropic"]:
        """
        根据 api_type 返回对应的客户端实例（缓存）。
        对 _NATIVE_SDK_PROVIDERS 中的厂商返回原生 SDK 客户端
        （目前仅 anthropic -> AsyncAnthropic），其余返回 AsyncOpenAI。
        """
        if api_type not in self._clients:
            self._clients[api_type] = self._build_client(api_type)
        return self._clients[api_type]


# 全局单例
api_client = APIClient()

# 导出
__all__ = ["api_client", "APIClient"]
