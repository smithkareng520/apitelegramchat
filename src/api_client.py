# api_client.py
"""
统一 API 客户端工厂（配置驱动）
支持通过 config.py 中的 PROVIDERS 字典动态添加新厂商，也支持每个模型
单独覆盖端点（base_url / api_key_env / 协议），详见
config.get_effective_endpoint()。

安全改进：所有 API Key 从 config 模块的变量中读取（而非 os.environ），
配合 config.py 的 scrub_environment() 实现环境变量完全清洗。
"""

import logging
import os
import httpx
from typing import Any, Dict, Optional, Union, cast
from openai import AsyncOpenAI

try:
    # 可选依赖：仅 anthropic 协议需要。未安装时其余协议完全不受影响，
    # 只有实际请求 anthropic 客户端时才会报错（而不是在导入期整体失败）。
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - 依赖缺失时的降级路径
    AsyncAnthropic = None  # type: ignore[misc,assignment]

# 从 config 导入厂商配置、端点合并函数以及所有 API Key 变量
from config import PROVIDERS, get_effective_endpoint, EffectiveEndpoint, ModelConfig
import config as app_config

logger = logging.getLogger(__name__)

# 使用原生 SDK（而非 AsyncOpenAI）的协议集合。新增协议只需加进这里 +
# config.py 的 ProviderConfig/ModelConfig 的 dedicated_loop_kind，
# _build_client 会自动分流，其余协议的构造逻辑不受影响。
# 注意：这里判断的是"有效协议"（dedicated_loop_kind），而不是 provider
# 名字——同一个 provider 壳下的不同模型可能通过端点覆盖各自声明不同的
# dedicated_loop_kind（见 get_effective_endpoint）。
_NATIVE_SDK_LOOP_KINDS = {"anthropic_native"}


class APIClient:
    """
    统一管理所有第三方 API 客户端。
    根据"有效端点"（EffectiveEndpoint，由 provider 默认值与模型级覆盖
    合并得到）动态创建并缓存客户端实例：
      - 默认协议：AsyncOpenAI（OpenAI 兼容协议，逻辑与之前完全一致）
      - dedicated_loop_kind="anthropic_native" 的端点：AsyncAnthropic

    客户端按"模型 ID"缓存（而非按 provider 缓存）：因为现在允许同一个
    provider 下的不同模型分别覆盖 base_url/api_key_env/协议，若仍按
    provider 缓存，第二个模型会错误复用第一个模型建好的客户端（连去
    第一个模型的端点）。model_id 在 SUPPORTED_MODELS 中天然唯一，用它
    做缓存 key 不会引入额外开销——未做任何端点覆盖的模型，合并结果与
    厂商默认完全一致，多个模型各自持有一个指向同一 base_url 的独立
    client 实例，除了多几个 httpx 连接池外无实质差别。

    添加新的 OpenAI 兼容厂商只需在 config.py 的 PROVIDERS 中配置；
    要让某个模型改用不同端点/协议，在该模型的 make_model_config(...)
    调用里加端点覆盖参数即可，无需修改本文件。
    """

    def __init__(self) -> None:
        self._clients: Dict[str, Union[AsyncOpenAI, "AsyncAnthropic"]] = {}
        self._providers = PROVIDERS  # 引用配置（仍供 fallback/未知厂商场景使用）

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

    def _build_native_client(self, endpoint: EffectiveEndpoint) -> "AsyncAnthropic":
        """
        构建原生 SDK 客户端（目前仅 anthropic_native）。与 _build_client 的
        AsyncOpenAI 分支完全独立，互不影响。
        """
        if endpoint.dedicated_loop_kind == "anthropic_native":
            if AsyncAnthropic is None:
                raise ValueError(
                    "未安装 anthropic 包，无法创建 Anthropic 客户端。"
                    "请在 requirements.txt / pyproject.toml 中确认 anthropic 依赖已安装。"
                )
            api_key = self._get_api_key(endpoint.api_key_env)
            if not api_key:
                raise ValueError(f"缺少 API Key: {endpoint.api_key_env}，请设置环境变量")
            logger.debug(f"创建 {endpoint.name} 原生客户端 base_url={endpoint.base_url}")
            kwargs: dict[str, Any] = {}
            # 仅当端点覆盖了 base_url 且不同于 Anthropic 官方默认时才显式传入，
            # 否则沿用 AsyncAnthropic SDK 自带的官方默认值，行为与此前完全一致。
            if endpoint.base_url and endpoint.base_url != "https://api.anthropic.com":
                kwargs["base_url"] = endpoint.base_url
            if endpoint.default_headers:
                kwargs["default_headers"] = endpoint.default_headers
            return AsyncAnthropic(
                api_key=api_key,
                # 与 OpenAI 兼容客户端保持相近的超时预算：连接短、流读取
                # 放宽到 300s（agentic 多轮工具调用后首个事件可能较晚）。
                # cast：SDK 存根引用的 httpx 类型对象与本环境安装的 httpx
                # 不同源（httpx2），运行时传入的是同一个 httpx.Timeout 实例。
                timeout=cast(Any, httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=60.0)),
                **kwargs,
            )
        raise ValueError(f"未知的原生 SDK 协议: {endpoint.dedicated_loop_kind}")

    def _build_client(self, endpoint: EffectiveEndpoint) -> Union[AsyncOpenAI, "AsyncAnthropic"]:
        """
        根据"有效端点"配置构建客户端。如果配置缺失或 API Key 为空，抛出 ValueError。
        """
        # anthropic_native 等原生 SDK 协议走独立分支，完全不影响以下
        # AsyncOpenAI 构造逻辑（现有厂商行为零变化）。
        if endpoint.dedicated_loop_kind in _NATIVE_SDK_LOOP_KINDS:
            return self._build_native_client(endpoint)

        api_key = self._get_api_key(endpoint.api_key_env)
        if not api_key:
            raise ValueError(f"缺少 API Key: {endpoint.api_key_env}，请设置环境变量")

        headers = endpoint.default_headers or {}

        logger.debug(f"创建 {endpoint.name} 客户端，base_url={endpoint.base_url}")

        # 禁用 OpenAI SDK 的隐式自动重试。SDK 默认会依据服务端 Retry-After
        # 睡眠，可能出现“Retrying request ... in 60.000000 seconds”，并且会与
        # agentic_loops 的首增量超时重试叠加。429/5xx 由上层统一处理，避免一次
        # 请求在 SDK 内部无感等待数十秒后才返回。
        try:
            sdk_max_retries = max(0, int(os.getenv("OPENAI_SDK_MAX_RETRIES", "0")))
        except (TypeError, ValueError):
            sdk_max_retries = 0

        return AsyncOpenAI(
            base_url=endpoint.base_url,
            api_key=api_key,
            max_retries=sdk_max_retries,
            # Agent 在多轮工具调用后，下一轮 SSE 的首个事件可能显著晚于普通聊天。
            # 使用分项超时：连接保持短，流读取允许 300 秒，避免 90 秒默认值中断长任务。
            # cast：SDK 存根引用的 httpx 类型对象与本环境安装的 httpx 不同源，
            # 运行时传入的就是标准 httpx.Timeout 实例。
            timeout=cast(Any, httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=60.0)),
            default_headers=headers,
        )

    def get_client_for_model(self, model_info: ModelConfig) -> Union[AsyncOpenAI, "AsyncAnthropic"]:
        """
        【推荐入口】根据具体的 ModelConfig 返回对应客户端实例（按 model_id 缓存）。
        自动合并该模型的端点覆盖（base_url/api_key_env/协议等，见
        config.get_effective_endpoint），因此同一 provider 下配置了不同
        中转端点的模型会拿到各自独立、指向各自端点的客户端。
        """
        model_id = getattr(model_info, "model_id", None) or str(model_info)
        if model_id not in self._clients:
            endpoint = get_effective_endpoint(model_info)
            self._clients[model_id] = self._build_client(endpoint)
        return self._clients[model_id]

    def get_client(self, api_type: str) -> Union[AsyncOpenAI, "AsyncAnthropic"]:
        """
        【向后兼容 / 无模型级覆盖场景】根据厂商名 api_type 返回客户端实例
        （按厂商名缓存，等价于该厂商在 PROVIDERS 中的默认端点）。

        注意：如果某个模型对该厂商做了端点覆盖，请改用 get_client_for_model(
        model_info)，否则会拿到厂商默认端点的客户端而非模型覆盖后的端点。
        本方法仅在明确知道当前厂商下所有模型都未做端点覆盖时安全使用
        （如子系统只按厂商粒度工作、拿不到具体 model_info 的场景）。
        """
        if api_type not in self._clients:
            base = self._providers.get(api_type)
            if not base:
                raise ValueError(f"未知厂商: {api_type}，请检查 config.py 中的 PROVIDERS")
            endpoint = EffectiveEndpoint(
                provider=api_type,
                name=base.name,
                base_url=base.base_url,
                api_key_env=base.api_key_env,
                default_headers=base.default_headers or {},
                dedicated_loop_kind=base.dedicated_loop_kind,
                supports_prompt_cache=base.supports_prompt_cache,
                vision_prefer_url=base.vision_prefer_url,
                session_affinity=base.session_affinity,
            )
            self._clients[api_type] = self._build_client(endpoint)
        return self._clients[api_type]


# 全局单例
api_client = APIClient()

# 导出
__all__ = ["api_client", "APIClient"]
