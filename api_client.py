# api_client.py
"""
统一 API 客户端工厂（配置驱动）
支持通过 config.py 中的 PROVIDERS 字典动态添加新厂商

安全改进：所有 API Key 从 config 模块的变量中读取（而非 os.environ），
配合 config.py 的 scrub_environment() 实现环境变量完全清洗。
"""

import logging
from typing import Dict, Optional
from openai import AsyncOpenAI

# 从 config 导入厂商配置、ProviderConfig 以及所有 API Key 变量
from config import PROVIDERS, ProviderConfig
import config as app_config

logger = logging.getLogger(__name__)


class APIClient:
    """
    统一管理所有第三方 API 客户端。
    根据 api_type 动态创建并缓存 AsyncOpenAI 实例。
    添加新厂商只需在 config.py 的 PROVIDERS 中配置，无需修改本文件。
    """

    def __init__(self):
        self._clients: Dict[str, AsyncOpenAI] = {}
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

    def _build_client(self, provider: str) -> AsyncOpenAI:
        """
        根据厂商配置构建 AsyncOpenAI 客户端
        如果配置缺失或 API Key 为空，抛出 ValueError
        """
        config = self._providers.get(provider)
        if not config:
            raise ValueError(f"未知厂商: {provider}，请检查 config.py 中的 PROVIDERS")

        api_key = self._get_api_key(config.api_key_env)
        if not api_key:
            raise ValueError(f"缺少 API Key: {config.api_key_env}，请设置环境变量")

        # 默认 headers（如有）
        headers = config.default_headers or {}

        logger.debug(f"创建 {config.name} 客户端，base_url={config.base_url}")

        return AsyncOpenAI(
            base_url=config.base_url,
            api_key=api_key,
            timeout=90,  # 可调整
            default_headers=headers,
        )

    def get_client(self, api_type: str) -> AsyncOpenAI:
        """
        根据 api_type 返回对应的 AsyncOpenAI 实例（缓存）
        """
        if api_type not in self._clients:
            self._clients[api_type] = self._build_client(api_type)
        return self._clients[api_type]

    # -------------------- 向后兼容的旧方法 --------------------
    # 以下方法保留以兼容现有代码，但内部统一使用 get_client

    def get_openrouter(self) -> AsyncOpenAI:
        return self.get_client("openrouter")

    def get_gemini(self) -> AsyncOpenAI:
        return self.get_client("gemini")

    def get_grok(self) -> AsyncOpenAI:
        return self.get_client("grok")

    def get_deepseek(self) -> AsyncOpenAI:
        return self.get_client("deepseek")

    # -------------------- 可选：获取所有已配置厂商 --------------------
    def list_providers(self) -> list:
        """返回所有已配置的厂商名称列表"""
        return list(self._providers.keys())


# 全局单例
api_client = APIClient()

# 导出
__all__ = ["api_client", "APIClient"]
