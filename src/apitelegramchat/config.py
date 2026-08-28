# config.py
import os
import sys
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, Optional

# ---------- 日志 ----------
logger = logging.getLogger(__name__)

# ---------- 环境变量 ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
# OpenRouter 全局路由偏好：默认价格优先、允许自动回退；可用环境变量覆盖。
OPENROUTER_PROVIDER_SORT = (os.getenv("OPENROUTER_PROVIDER_SORT") or "price").strip() or "price"
OPENROUTER_ALLOW_FALLBACKS = os.getenv("OPENROUTER_ALLOW_FALLBACKS", "true").strip().lower() in {"1", "true", "yes", "on"}
OPENROUTER_REQUIRE_PARAMETERS = os.getenv("OPENROUTER_REQUIRE_PARAMETERS", "false").strip().lower() in {"1", "true", "yes", "on"}
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
MODELSCOPE_API_KEY = os.getenv("MODELSCOPE_API_KEY", "")
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "")

# ---------- 高德地图 MCP 服务（@amap/amap-maps on ModelScope）----------
# 通过 streamable_http 调用，使用 Bearer token 鉴权。
# 替代了原先的 amap_integration.py 直接调用高德 Web 服务 API 的方式。
# 未配置 GAODE_MCP_TOKEN 或 GAODE_MCP_URL 时该 MCP 服务不可用（mcp_client.py 会跳过注册）。
#
# 注意：GAODE_MCP_URL 不设默认值——之前的默认值硬编码了一个具体的
# ModelScope MCP 实例路径（.../3331c36972ff42/mcp），这类路径段通常绑定
# 到某个具体账号的私有实例。一旦部署方忘记覆盖该环境变量，就会在不知情
# 的情况下把请求发往别人的实例（可能是私有、限流或按量计费的），且大概率
# 连不通或返回权限错误。改为必须显式配置（与 SERPER_API_KEY 同口径）。
GAODE_MCP_ENABLED = os.getenv("GAODE_MCP_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
GAODE_MCP_URL = (os.getenv("GAODE_MCP_URL") or "").strip()
GAODE_MCP_TOKEN = (os.getenv("GAODE_MCP_TOKEN") or "").strip()

# ---------- 网页搜索：Serper 官方 REST API ----------
# 直接调用 https://google.serper.dev/{search,images,videos,lens}，使用
# X-API-KEY 头鉴权。Key 从 https://serper.dev 注册并获取，配置在
# Render Environment 中作为 secret。一个 key 即可同时支持 4 种模式。
SERPER_API_KEY = (os.getenv("SERPER_API_KEY") or "").strip()
# 可选：单次请求超时（秒）；默认 12s 与外层 web_search 工具超时（45s）预算匹配。
# 真正赋值在 _positive_float_env 定义之后（见下文 SERPER_API_TIMEOUT_RESOLVED）。


WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")
_RAW_WEBHOOK_URL = os.getenv("WEBHOOK_URL") or ""
# Webhook 注册由部署平台/运维侧完成（setWebhook 时自行拼接 ?token=…），
# 应用内只消费原始 WEBHOOK_URL（见 validate_runtime_config 与启动配置汇总）。

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

# ---------- 公共：环境变量安全解析工具 ----------
# 必须在使用前定义（LOG_TRUNCATE_LIMIT / MAX_CONCURRENT_TOOLS 等都依赖）。
# 合法推理努力档位（OpenAI gpt-5 / Gemini 3 / Claude / OpenRouter 通用口径）
VALID_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "none"}


def _positive_float_env(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _positive_int_env(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# 现在 _positive_float_env 已定义，可以安全赋值。
SERPER_API_TIMEOUT = _positive_float_env("SERPER_API_TIMEOUT", 12.0, 1.0)


# ---------- 日志截断配置 ----------
LOG_TRUNCATE_LIMIT = _positive_int_env("LOG_TRUNCATE_LIMIT", 5000, 1)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ---------- 必需环境变量检查 ----------
def validate_runtime_config(*, strict: bool = False) -> None:
    """
    默认保持导入安全：MCP server、离线测试和单元测试可以在无 Telegram 环境变量时导入。
    只有显式要求时才抛出缺失配置错误。
    """
    if not strict:
        return
    required = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "WEBHOOK_TOKEN": WEBHOOK_TOKEN,
        "WEBHOOK_URL": _RAW_WEBHOOK_URL,
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error(f"缺少必需的环境变量: {', '.join(missing)}")
        raise RuntimeError(f"缺少必需的环境变量: {', '.join(missing)}")

if os.getenv("APITELEGRAMCHAT_REQUIRE_STRICT_CONFIG", "0") in {"1", "true", "yes", "on"}:
    try:
        validate_runtime_config(strict=True)
    except RuntimeError as exc:
        # 导入期 logger 还没配置 basicConfig，print 到 stderr 兜底。
        print(f"[apitelegramchat.config] {exc}", file=sys.stderr)
        raise

# ---------- 全局锁 ----------
global_lock = asyncio.Lock()

# ---------- 角色相关 ----------
SUPPORTED_ROLES = ["china", "think", "neko_catgirl", "succubus", "isla"]

# =============================================================================
# 配置驱动架构：厂商定义 + 模型定义
# =============================================================================

@dataclass
class ProviderConfig:
    """厂商配置"""
    name: str
    base_url: str
    api_key_env: str
    default_headers: Optional[Dict[str, str]] = None
    # 是否使用专用循环（如 Gemini 非流式特殊处理）
    use_dedicated_loop: bool = False
    # 是否支持 Prompt Caching（仅部分厂商需要显式标记）
    supports_prompt_cache: bool = False
    # 视觉输入是否需要"公开可访问 HTTP URL"而非 data:image/...;base64,... 内联格式。
    # 部分 OpenAI 兼容网关（如 Agnes）官方文档明确只接受 image_url 中的公开 URL，
    # 内联 base64 会被静默忽略甚至报 4xx。开启后，会在 _resolve_multimodal_content
    # 里优先用 R2 公开 URL（不泄露 Telegram bot token），R2 不可用时回退 base64。
    vision_prefer_url: bool = False


@dataclass
class ModelConfig:
    """
    模型配置，所有字段与原有保持一致，新增 provider 和 max_context 字段。
    如果某些能力未显式指定，则从厂商默认值继承。
    """
    model_id: str               # 完整的模型 ID，如 "google/gemini-2.5-flash"
    provider: str               # 对应 PROVIDERS 的 key
    name: str                   # 显示名称
    vision: Optional[bool] = None
    audio: Optional[bool] = None
    # 视频输入（video understanding）能力：模型能否直接“看”视频内容。
    # 注意与 native_video（视频生成输出）区分：前者是输入模态，后者是
    # 生成模态。视频输入通过 OpenAI 兼容协议的 video_url content part
    # 传递（OpenRouter / vLLM / LiteLLM 等均为该事实标准），且由于视频
    # 体积远大于图片，base64 内联容易触发网关请求体上限，因此统一优先
    # 走 R2 公开 URL（见 attachment_content._resolve_r2_public_url_for_video）。
    video: Optional[bool] = None
    supports_tools: Optional[bool] = None
    native_image: Optional[bool] = None
    native_document: Optional[bool] = None
    native_video: Optional[bool] = None
    supports_sampling: Optional[bool] = None
    supports_prompt_cache: Optional[bool] = None 
    max_output_tokens: Optional[int] = None
    max_context: Optional[int] = None  # <=== 【新增】最大上下文窗口

    # ===================== 推理控制（思考开关 / 努力档位 / token 上限）=====
    # 三者均可独立配置；None = 不向 API 发送任何推理控制参数（跟随模型默认）。
    # reasoning_enabled:  显式开/关思考（GLM thinking.type / ModelScope
    #                     enable_thinking / OpenRouter reasoning.enabled /
    #                     Gemini thinkingBudget=0 关闭）
    # reasoning_effort:   努力档位 "minimal"/"low"/"medium"/"high"（"none" 仅
    #                     部分模型支持，会透传）
    # reasoning_max_tokens: 推理 token 预算（OpenRouter reasoning.max_tokens /
    #                     Gemini thinkingBudget / ModelScope thinking_budget）
    reasoning_enabled: Optional[bool] = None
    reasoning_effort: Optional[str] = None
    reasoning_max_tokens: Optional[int] = None

    # ===================== 采样参数 =====================
    # None = 不发送该字段，走供应商默认（供应商默认采样已按模型调优）；
    # 数值 = 按模型覆盖。某模型完全不支持采样时用 supports_sampling=False。
    temperature: Optional[float] = None
    top_p: Optional[float] = None

    @property
    def api_type(self) -> str:
        return self.provider

    def get(self, key, default=None):
        return getattr(self, key, default)


# =============================================================================
# 厂商配置表
# =============================================================================
PROVIDERS: Dict[str, ProviderConfig] = {
    "openrouter": ProviderConfig(
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        supports_prompt_cache=False,  # OpenAI 等自动缓存
    ),
    "modelscope": ProviderConfig(
        name="ModelScope",
        base_url="https://api-inference.modelscope.cn/v1",
        api_key_env="MODELSCOPE_API_KEY",
        supports_prompt_cache=False,  # OpenAI 等自动缓存
    ),
    "gemini": ProviderConfig(
        name="Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        use_dedicated_loop=True,      # Gemini 使用非流式专用循环
        supports_prompt_cache=False,  # Gemini 隐式缓存，无需标记
    ),
    "grok": ProviderConfig(
        name="Grok",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        supports_prompt_cache=False,
    ),
    "deepseek": ProviderConfig(
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        supports_prompt_cache=False,
    ),
    "glm": ProviderConfig(
        name="glm",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="GLM_API_KEY",
        supports_prompt_cache=False,
    ),
    "agnes": ProviderConfig(
        name="agnes",
        base_url="https://apihub.agnes-ai.com/v1",
        api_key_env="AGNES_API_KEY",
        supports_prompt_cache=False,
        # Agnes 官方文档明确只接受 image_url 中的公开 URL（不接受 data: base64），
        # 因此 _resolve_multimodal_content 会优先用 R2 公开 URL，R2 不可用时回退 base64。
        vision_prefer_url=True,
    ),
}


# =============================================================================
# 厂商默认能力（模型未覆盖时使用）
# =============================================================================
_PROVIDER_DEFAULTS: Dict[str, Dict] = {
    "openrouter": {
        "vision": False,
        "audio": False,
        "video": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "native_video": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "temperature": None,          # None -> 不发送，走供应商默认
        "top_p": None,                # None -> 不发送，走供应商默认
        "reasoning_enabled": None,    # None -> 不发送推理控制参数
        "reasoning_effort": None,
        "reasoning_max_tokens": None,
        "max_output_tokens": 8192,
        "max_context": 128000,
    },
    "modelscope": {
        "vision": False,
        "audio": False,
        "video": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "temperature": None,          # None -> 不发送，走供应商默认
        "top_p": None,                # None -> 不发送，走供应商默认
        "reasoning_enabled": None,    # None -> 不发送推理控制参数
        "reasoning_effort": None,
        "reasoning_max_tokens": None,
        "max_output_tokens": 8192,
        "max_context": 128000,
    },
    "gemini": {
        "vision": True,
        "audio": False,
        "video": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "temperature": None,          # None -> 不发送，走供应商默认
        "top_p": None,                # None -> 不发送，走供应商默认
        "reasoning_enabled": None,    # None -> 不发送推理控制参数
        "reasoning_effort": None,
        "reasoning_max_tokens": None,
        "max_output_tokens": 8192,
        "max_context": 1000000,
    },
    "grok": {
        "vision": False,
        "audio": False,
        "video": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "temperature": None,          # None -> 不发送，走供应商默认
        "top_p": None,                # None -> 不发送，走供应商默认
        "reasoning_enabled": None,    # None -> 不发送推理控制参数
        "reasoning_effort": None,
        "reasoning_max_tokens": None,
        "max_output_tokens": 8192,
        "max_context": 128000,
    },
    "deepseek": {
        "vision": False,
        "audio": False,
        "video": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "temperature": None,          # None -> 不发送，走供应商默认
        "top_p": None,                # None -> 不发送，走供应商默认
        "reasoning_enabled": None,    # None -> 不发送推理控制参数
        "reasoning_effort": None,
        "reasoning_max_tokens": None,
        "max_output_tokens": 8192,
        "max_context": 128000,
    },
    "glm": {
        "vision": False,
        "audio": False,
        "video": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "temperature": None,          # None -> 不发送，走供应商默认
        "top_p": None,                # None -> 不发送，走供应商默认
        "reasoning_enabled": None,    # None -> 不发送推理控制参数
        "reasoning_effort": None,
        "reasoning_max_tokens": None,
        "max_output_tokens": 8192,
        "max_context": 128000,
    },
    "agnes": {
        "vision": False,
        "audio": False,
        "video": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "native_video": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "temperature": None,          # None -> 不发送，走供应商默认
        "top_p": None,                # None -> 不发送，走供应商默认
        "reasoning_enabled": None,    # None -> 不发送推理控制参数
        "reasoning_effort": None,
        "reasoning_max_tokens": None,
        "max_output_tokens": 8192,
        "max_context": 128000,
    },
}


def _merge_with_defaults(provider: str, overrides: dict) -> dict:
    """合并厂商默认值和模型覆盖值"""
    defaults = _PROVIDER_DEFAULTS.get(provider, {})
    merged = defaults.copy()
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    return merged


def get_openrouter_provider_preferences() -> dict:
    """返回所有 OpenRouter 请求共用的路由偏好。"""
    prefs = {
        "sort": OPENROUTER_PROVIDER_SORT,
        "allow_fallbacks": OPENROUTER_ALLOW_FALLBACKS,
    }
    if OPENROUTER_REQUIRE_PARAMETERS:
        prefs["require_parameters"] = True
    return prefs


def make_model_config(
    model_id: str,
    provider: str,
    name: str,
    **kwargs
) -> ModelConfig:
    """工厂函数：根据 provider 和覆盖项创建 ModelConfig"""
    merged = _merge_with_defaults(provider, kwargs)

    # 推理努力档位校验：拼错会在运行期被网关 400，尽早暴露。
    effort = merged.get("reasoning_effort")
    if effort is not None:
        effort = str(effort).strip().lower()
        if effort not in VALID_REASONING_EFFORTS:
            raise ValueError(
                f"模型 {model_id} 的 reasoning_effort={effort!r} 无效，"
                f"合法值: {sorted(VALID_REASONING_EFFORTS)}"
            )
        merged["reasoning_effort"] = effort

    # 推理预算下限：负数/0 没有意义。
    budget = merged.get("reasoning_max_tokens")
    if budget is not None and int(budget) < 1:
        raise ValueError(f"模型 {model_id} 的 reasoning_max_tokens={budget!r} 必须 >= 1")

    # OpenRouter 的模型 ID 需要完整的 openrouter/<author>/<slug> 形式。
    normalized_model_id = model_id
    if provider == "openrouter" and not model_id.startswith("openrouter/"):
        normalized_model_id = f"openrouter/{model_id}"

    return ModelConfig(
        model_id=normalized_model_id,
        provider=provider,
        name=name,
        vision=merged.get("vision"),
        audio=merged.get("audio"),
        video=merged.get("video"),
        supports_tools=merged.get("supports_tools"),
        native_image=merged.get("native_image"),
        native_document=merged.get("native_document"),
        native_video=merged.get("native_video"),
        supports_sampling=merged.get("supports_sampling"),
        supports_prompt_cache=merged.get("supports_prompt_cache"),
        max_output_tokens=merged.get("max_output_tokens", 8192),
        max_context=merged.get("max_context", 128000),
        reasoning_enabled=merged.get("reasoning_enabled"),
        reasoning_effort=merged.get("reasoning_effort"),
        reasoning_max_tokens=(int(budget) if budget is not None else None),
        temperature=merged.get("temperature"),
        top_p=merged.get("top_p"),
    )


# =============================================================================
# 统一参数出口：所有 agentic 循环（主循环 / subagent / 回退 / 总结请求）
# 一律通过这两个函数获取采样与推理参数，禁止在循环内硬编码。
# =============================================================================
def get_sampling_params(model_info) -> dict:
    """
    返回应并入 chat.completions.create 的采样参数（temperature / top_p）。

    规则：
      - model_info 为 None 或 supports_sampling=False -> 返回 {}（不发送采样参数）
      - 字段为 None -> 不发送该字段，走供应商默认；
      - 字段为数值 -> 按模型覆盖。
    """
    if not model_info:
        return {}
    if not getattr(model_info, "supports_sampling", True):
        return {}
    params: Dict[str, float] = {}
    if model_info.temperature is not None:
        params["temperature"] = model_info.temperature
    if model_info.top_p is not None:
        params["top_p"] = model_info.top_p
    return params


_REASONING_NOOP = ({}, {})


def get_reasoning_request_fields(model_info, api_label: str = "") -> tuple:
    """
    根据模型配置与厂商，返回推理控制（思考开关/努力档位/推理预算）参数。

    返回 (top_level_params, extra_body_fields)：
      - top_level_params: 直接并入 create()/payload 顶层的字段
        （如 reasoning_effort）
      - extra_body_fields: 需并入 extra_body（OpenAI SDK）或原始 JSON body
        顶层的字段（如 reasoning/thinking/enable_thinking/google）

    未配置任何推理控制时返回 ({}, {})。厂商映射：
      openrouter  -> extra_body.reasoning = {enabled?, effort?, max_tokens?}
                     （OpenRouter 统一推理接口，会自动转换到具体后端）
      gemini      -> 顶层 reasoning_effort + extra_body.google.thinking_config
                     .thinkingBudget（官方 OpenAI 兼容层；enabled=False 映射
                     thinkingBudget=0 关闭思考）
      glm         -> extra_body.thinking = {"type": "enabled"/"disabled"}
                     （智谱官方 v4 接口；预算暂不支持，静默忽略）
      modelscope  -> extra_body.enable_thinking / thinking_budget
                     （DashScope 风格，Qwen/GLM/DeepSeek 混合思考模型通用）
      其他        -> 顶层 reasoning_effort（OpenAI gpt-5 / xAI 等兼容网关
                     的事实标准；仅当显式配置 effort 时发送）
    """
    if not model_info:
        return _REASONING_NOOP
    enabled = getattr(model_info, "reasoning_enabled", None)
    effort = getattr(model_info, "reasoning_effort", None)
    budget = getattr(model_info, "reasoning_max_tokens", None)
    if enabled is None and effort is None and budget is None:
        return _REASONING_NOOP

    provider = (api_label or getattr(model_info, "provider", "") or "").strip().lower()

    if provider == "openrouter":
        reasoning: Dict[str, object] = {}
        if enabled is not None:
            reasoning["enabled"] = bool(enabled)
        if effort is not None:
            reasoning["effort"] = effort
        if budget is not None:
            reasoning["max_tokens"] = int(budget)
        return ({}, {"reasoning": reasoning})

    if provider == "gemini":
        top_level: Dict[str, object] = {}
        google_cfg: Dict[str, object] = {}
        if effort is not None:
            top_level["reasoning_effort"] = effort
        if enabled is False:
            google_cfg["thinkingBudget"] = 0
        elif budget is not None:
            google_cfg["thinkingBudget"] = int(budget)
        if google_cfg:
            return (top_level, {"google": {"thinking_config": google_cfg}})
        return (top_level, {})

    if provider == "glm":
        thinking_type = "enabled" if enabled in (None, True) else "disabled"
        thinking: Dict[str, object] = {"type": thinking_type}
        # GLM 官方接口对未知字段宽容，但为稳妥起见仅在显式配置时携带预算。
        if budget is not None:
            thinking["max_reasoning_tokens"] = int(budget)
        return ({}, {"thinking": thinking})

    if provider == "modelscope":
        extra: Dict[str, object] = {}
        if enabled is not None:
            extra["enable_thinking"] = bool(enabled)
        if budget is not None:
            extra["thinking_budget"] = int(budget)
        return ({}, extra)

    # 其他 OpenAI 兼容厂商：只透传 effort（能力不明的网关不发未知字段）。
    if effort is not None:
        return ({"reasoning_effort": effort}, {})
    return _REASONING_NOOP


# =============================================================================
# 模型列表（所有支持的模型）
# =============================================================================
SUPPORTED_MODELS: Dict[str, ModelConfig] = {}

# ---------- OpenRouter 模型 ----------
SUPPORTED_MODELS["dots-studio/dots-3-note-preview:free"] = make_model_config(
    model_id="dots-studio/dots-3-note-preview:free",
    provider="openrouter",
    name="Dots-3-Note Preview Free",
    vision=True,
    max_context=512000,
    # 笔记型预览模型：不发送推理控制（预览期能力未知），采样不发送、走供应商默认。
)
SUPPORTED_MODELS["poolside/laguna-s-2.1:free"] = make_model_config(
    model_id="poolside/laguna-s-2.1:free",
    provider="openrouter",
    name="Laguna S 2.1 Free",
    max_context=262000,
    # 代码模型，免费档：不发送推理控制与采样参数，走供应商默认。
)
SUPPORTED_MODELS["anthropic/claude-sonnet-5"] = make_model_config(
    model_id="anthropic/claude-sonnet-5",
    provider="openrouter",
    name="Claude Sonnet 5",
    vision=True,
    native_document=True,
    supports_prompt_cache=True,
    max_context=1000000,
    # 扩展思考：高努力档位，经 OpenRouter 统一 reasoning 接口下发。
    reasoning_enabled=True,
    reasoning_effort="high",
    # Anthropic 思考模式官方要求 temperature=1.0，且不建议调整 top_p；
    # top_p 不配置即不发送，走供应商默认。
    temperature=1.0,
)

# ---------- Agnes 免费模型 ----------
# (duplicate gemma entry removed)
SUPPORTED_MODELS["agnes-2.5-flash"] = make_model_config(
    model_id="agnes-2.5-flash",
    provider="agnes",
    name="Agnes 2.5 Flash",
    max_context=512000,
    vision=True,
    # 默认主力模型：网关对推理/采样参数的支持未公开，保守起见两者都不发送，
    # 完全走供应商默认。确认网关支持后可在此显式开启。
)

# ---------- ModelScope 免费模型 ----------
SUPPORTED_MODELS["deepseek-ai/DeepSeek-V4-Flash-0731"] = make_model_config(
    model_id="deepseek-ai/DeepSeek-V4-Flash-0731",
    provider="modelscope",
    name="Deepseek V4 Flash",
    max_context=1000000,
    # 混合推理模型：开启思考（DashScope 风格 enable_thinking）。
    # DeepSeek 思考模式建议 temperature 0.5-0.7，取 0.6；官方文档指出思考
    # 模式下 top_p 影响很小，不发送 top_p 走供应商默认。
    reasoning_enabled=True,
    temperature=0.6,
)

SUPPORTED_MODELS["ZhipuAI/GLM-5.2"] = make_model_config(
    model_id="ZhipuAI/GLM-5.2",
    provider="modelscope",
    name="GLM 5.2",
    max_context=1000000,
    # GLM 混合思考：ModelScope 通道开启思考，智谱官方建议思考模式
    # temperature=0.6；如需关闭改 reasoning_enabled=False。
    reasoning_enabled=True,
    temperature=0.6,
)

# ---------- Gemini 系列 ----------
SUPPORTED_MODELS["gemini-3.5-flash-lite"] = make_model_config(
    model_id="gemini-3.5-flash-lite",
    provider="gemini",
    name="Gemini 3.5 Flash-Lite",
    vision=True,
    max_context=1000000,
    # Flash-Lite 定位轻快：思考限制在低档，避免响应变慢。
    # Google 建议不调整 temperature（默认 1.0）；top_p 不配置即不发送。
    # 如需精确控制可改用 reasoning_max_tokens（映射 thinkingBudget）。
    reasoning_effort="low",
    temperature=1.0,
)
# ---------- GLM 系列 ----------
SUPPORTED_MODELS["GLM-4.6V-Flash"] = make_model_config(
    model_id="GLM-4.6V-Flash",
    provider="glm",
    name="GLM 4.6V Flash",
    vision=True,
    max_context=128000,
    # 智谱官方 v4 接口：thinking.type=enabled。视觉 Flash 免费档，
    # 思考默认开启可提升图表/文档理解准确率。
    # 智谱建议思考模式 temperature 0.5-0.7，取 0.6；top_p 不发送走默认。
    reasoning_enabled=True,
    temperature=0.6,
)
SUPPORTED_MODELS["GLM-4.7-Flash"] = make_model_config(
    model_id="GLM-4.7-Flash",
    provider="glm",
    name="GLM 4.7 Flash",
    max_context=200000,
    # 同上：思考开启，智谱思考模式建议 temperature=0.6。
    # 需要更快响应时改 reasoning_enabled=False。
    reasoning_enabled=True,
    temperature=0.6,
)

# ---------- 图像生成模型 ----------
SUPPORTED_MODELS["Qwen/Qwen-Image-Edit"] = make_model_config(
    model_id="Qwen/Qwen-Image-Edit",
    provider="modelscope",
    name="Qwen Image Edit",
    vision=True,
    native_image=True,
    max_context=32768,
    max_output_tokens=4000,
)
SUPPORTED_MODELS["agnes-image-2.1-flash"] = make_model_config(
    model_id="agnes-image-2.1-flash",
    provider="agnes",
    name="Agnes Image 2.1 Flash",
    max_context=32768,
    max_output_tokens=4000,
)
SUPPORTED_MODELS["Tongyi-MAI/Z-Image-Turbo"] = make_model_config(
    model_id="Tongyi-MAI/Z-Image-Turbo",
    provider="modelscope",
    name="Z Image Turbo",
    native_image=True,
    max_context=32768,
    max_output_tokens=4000,
)
SUPPORTED_MODELS["google/gemini-3.1-flash-lite-image"] = make_model_config(
    model_id="google/gemini-3.1-flash-lite-image",
    provider="openrouter",
    name="Gemini 3.1 Flash Lite Image",
    native_image=True,
    vision=True,
    supports_tools=False,
    max_context=131000,
)
SUPPORTED_MODELS["google/gemini-3-pro-image-preview"] = make_model_config(
    model_id="google/gemini-3-pro-image-preview",
    provider="openrouter",
    name="Gemini 3 Pro Image Preview",
    native_image=True,
    vision=True,
    supports_tools=False,
    max_context=66000,
)
SUPPORTED_MODELS["bytedance-seed/seedream-4.5"] = make_model_config(
    model_id="bytedance-seed/seedream-4.5",
    provider="openrouter",
    name="Seedream 4.5",
    native_image=True,
    vision=True,
    supports_tools=False,
    max_context=4000,
    max_output_tokens=1024,
)

# ---------- 视频生成模型 ----------
SUPPORTED_MODELS["agnes-video-v2.0"] = make_model_config(
    model_id="agnes-video-v2.0",
    provider="agnes",
    name="Agnes Video V2.0",
    native_video=True,
    max_context=32768,
    max_output_tokens=4000,
)

# ========== 默认模型 ==========
DEFAULT_MODEL = "agnes-2.5-flash"
assert DEFAULT_MODEL in SUPPORTED_MODELS, f"默认模型 {DEFAULT_MODEL} 未定义"


# =============================================================================
# 白名单管理
# =============================================================================
WHITELIST_FILE = os.getenv("APITELEGRAMCHAT_WHITELIST_FILE") or "whitelist.txt"
ADMIN_USERS = ["dearella"]
WHITELIST_USERS = set()
_whitelist_lock = asyncio.Lock()

def _resolve_whitelist_path() -> str:
    """返回白名单文件路径，优先使用绝对路径，否则挂到 data_root 下。"""
    if os.path.isabs(WHITELIST_FILE):
        return WHITELIST_FILE
    try:
        from apitelegramchat.workspace_paths import data_root
        return str(data_root() / WHITELIST_FILE)
    except Exception:
        return WHITELIST_FILE

async def load_whitelist():
    global WHITELIST_USERS
    async with _whitelist_lock:
        try:
            with open(_resolve_whitelist_path(), "r", encoding="utf-8") as f:
                WHITELIST_USERS = {line.strip() for line in f if line.strip()}
        except FileNotFoundError:
            WHITELIST_USERS = set()
        except OSError:
            logger.warning("load_whitelist failed: %s", _resolve_whitelist_path(), exc_info=True)

async def save_whitelist():
    async with _whitelist_lock:
        try:
            with open(_resolve_whitelist_path(), "w", encoding="utf-8") as f:
                f.writelines(user + "\n" for user in sorted(WHITELIST_USERS))
        except OSError:
            logger.warning("save_whitelist failed: %s", _resolve_whitelist_path(), exc_info=True)

# ---------- 缓存 TTL ----------
CACHE_TTL = _positive_int_env("CACHE_TTL", 300, 10)
SEARCH_CACHE_TTL = _positive_int_env("SEARCH_CACHE_TTL", 300, 10)
FETCH_CACHE_TTL = _positive_int_env("FETCH_CACHE_TTL", 3600, 10)

# ---------- S3 / R2 配置 ----------
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")
R2_REGION = os.getenv("R2_REGION", "auto")

# ---------- 流式刷新阈值 ----------
# 草稿是用户感知 Agent 正在工作的唯一实时界面。默认值优先保证首字与
# 状态变更的可见性，同时仍低于 Telegram 草稿 API 的常规刷新频率。
STREAM_FLUSH_INTERVAL = _positive_float_env("STREAM_FLUSH_INTERVAL", 0.65, 0.25)
STREAM_SILENT_FORCE_FLUSH = _positive_float_env(
    "STREAM_SILENT_FORCE_FLUSH", 2.0, STREAM_FLUSH_INTERVAL
)

# ---------- 工具调用并发数 ----------
MAX_CONCURRENT_TOOLS = _positive_int_env("MAX_CONCURRENT_TOOLS", 16, 1)

# =============================================================================
# 安全补丁：读取后立即清洗敏感环境变量
# =============================================================================
_SENSITIVE_PATTERNS = (
    "TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD",
    "CREDENTIAL", "PRIVATE", "ACCESS", "WEBHOOK_TOKEN",
)
_SENSITIVE_EXACT = {
    "TELEGRAM_BOT_TOKEN",
    "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY",
    "XAI_API_KEY", "GROQ_API_KEY", "MODELSCOPE_API_KEY", "AGNES_API_KEY",
    "R2_ENDPOINT", "R2_ACCESS_KEY", "R2_SECRET_KEY",
    "R2_BUCKET_NAME", "R2_PUBLIC_URL", "R2_REGION",
    "SERPER_API_KEY", "GAODE_MCP_TOKEN",
    "WEBHOOK_TOKEN", "WEBHOOK_URL",
}

def scrub_environment() -> None:
    removed = []
    for key in list(os.environ.keys()):
        key_upper = key.upper()
        if key_upper in _SENSITIVE_EXACT:
            os.environ.pop(key, None)
            removed.append(key)
            continue
        for pattern in _SENSITIVE_PATTERNS:
            if pattern in key_upper:
                os.environ.pop(key, None)
                removed.append(key)
                break
    if removed:
        logger.info(f"🔒 Scrubbed {len(removed)} sensitive env vars: {', '.join(removed)}")

scrub_environment()
