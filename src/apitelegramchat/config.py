"""apitelegramchat 的集中式运行时配置。"""

# config.py
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Dict, Optional

# -----------------------------------------------------------------------------
# 日志
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# 环境变量
# -----------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GLM_API_KEY = os.getenv("GLM_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")

# OpenRouter 全局路由偏好：默认价格优先、允许自动回退；可用环境变量覆盖。
OPENROUTER_PROVIDER_SORT = (os.getenv("OPENROUTER_PROVIDER_SORT") or "price").strip() or "price"
OPENROUTER_ALLOW_FALLBACKS = os.getenv("OPENROUTER_ALLOW_FALLBACKS", "true").strip().lower() in {"1", "true", "yes", "on"}
OPENROUTER_REQUIRE_PARAMETERS = os.getenv("OPENROUTER_REQUIRE_PARAMETERS", "false").strip().lower() in {"1", "true", "yes", "on"}
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
MODELSCOPE_API_KEY = os.getenv("MODELSCOPE_API_KEY", "")
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "")
# Anthropic 官方 API（原生 Messages API，非 OpenAI 兼容协议）。
# 与其余厂商并存：其余厂商继续走 AsyncOpenAI + chat.completions.create，
# 互不影响；本 key 仅供 anthropic 厂商专用循环
# （ai/agentic_loops._agentic_loop_anthropic）使用。
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# XXTF 中转（https://xxtf.baby）：claude-opus-5（Anthropic 原生协议）与
# gpt-5.6-sol（OpenAI 协议）共用同一个 key，见下方 PROVIDERS["xxtf"] 与
# SUPPORTED_MODELS 中的模型定义（"XXTF 中转"注释块）。
XXTF_API_KEY = os.getenv("XXTF_API_KEY", "")


# ---------- 高德地图 MCP 服务（@amap/amap-maps on ModelScope）----------
# 通过 streamable_http 调用，使用 Bearer token 鉴权。
# 未配置 GAODE_MCP_TOKEN 或 GAODE_MCP_URL 时该 MCP 服务不可用（mcp_client.py 会跳过注册）。
GAODE_MCP_ENABLED = os.getenv("GAODE_MCP_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
GAODE_MCP_URL = (os.getenv("GAODE_MCP_URL") or "").strip()
GAODE_MCP_TOKEN = (os.getenv("GAODE_MCP_TOKEN") or "").strip()

# -----------------------------------------------------------------------------
# 网页搜索：Serper 官方 REST API
# -----------------------------------------------------------------------------
# 直接调用 https://google.serper.dev/{search,images,videos,lens}，使用
# X-API-KEY 头鉴权。Key 从 https://serper.dev 注册并获取，配置在
# Render Environment 中作为 secret。一个 key 即可同时支持 4 种模式。
SERPER_API_KEY = (os.getenv("SERPER_API_KEY", "") or "").strip()
# 可选：单次请求超时（秒）；默认 12s 与外层 web_search 工具超时（45s）预算匹配。
# 真正赋值在 _positive_float_env 定义之后（见下文 SERPER_API_TIMEOUT_RESOLVED）。


WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")
_RAW_WEBHOOK_URL = os.getenv("WEBHOOK_URL") or ""
# Webhook 注册采用"启动自愈"：应用启动时（app._startup_sync_webhook →
# webhook_sync.sync_webhook_on_startup）用 WEBHOOK_URL?token=WEBHOOK_TOKEN
# 幂等调用 setWebhook 重注册，并输出 getWebhookInfo 观测日志
# （pending_update_count / last_error_*），让积压可被观测。
# 注意：setWebhook 只修"未来的投递路由"，不影响 Telegram 侧已积压的
# update 队列；唯一清队手段是 drop_pending_updates=true（见下）。
# DROP_PENDING_ON_STARTUP=true 时，启动注册附带 drop_pending_updates=true，
# 在自愈注册的同时清空 Telegram 侧积压队列——停机/部署窗口内收到的消息
# 会被**永久丢弃**（不投递、不回复），仅当宁可丢消息也不愿迟到回复时开启。
DROP_PENDING_ON_STARTUP = os.getenv("DROP_PENDING_ON_STARTUP", "false").strip().lower() in {"1", "true", "yes", "on"}

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

# -----------------------------------------------------------------------------
# 公共：环境变量安全解析工具
# -----------------------------------------------------------------------------
# 必须在使用前定义（LOG_TRUNCATE_LIMIT / MAX_CONCURRENT_TOOLS 等都依赖）。
# 合法推理努力档位（OpenAI gpt-5 / Gemini 3 / Claude / OpenRouter 通用口径）
VALID_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max", "minimal"}
# 合法的协议循环标签（ProviderConfig.dedicated_loop_kind 默认值与
# ModelConfig.dedicated_loop_kind 覆盖字段共用同一取值域）。
# 单字段选择器："openai_compat" 为缺省协议（OpenAI 兼容 Chat Completions
# 循环），原生协议按需显式声明。
_VALID_DEDICATED_LOOP_KINDS = {"openai_compat", "gemini_native", "anthropic_native"}


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


# -----------------------------------------------------------------------------
# 日志截断配置
# -----------------------------------------------------------------------------
LOG_TRUNCATE_LIMIT = _positive_int_env("LOG_TRUNCATE_LIMIT", 5000, 1)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# -----------------------------------------------------------------------------
# 必需环境变量检查
# -----------------------------------------------------------------------------
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
        missing_text = ", ".join(missing)
        logger.error("缺少必需的环境变量: %s", missing_text)
        raise RuntimeError(f"缺少必需的环境变量: {missing_text}")

if os.getenv("APITELEGRAMCHAT_REQUIRE_STRICT_CONFIG", "0") in {"1", "true", "yes", "on"}:
    try:
        validate_runtime_config(strict=True)
    except RuntimeError as exc:
        # 导入期 logger 还没配置 basicConfig，print 到 stderr 兜底。
        print(f"[apitelegramchat.config] {exc}", file=sys.stderr)
        raise

# -----------------------------------------------------------------------------
# 角色相关
# -----------------------------------------------------------------------------
SUPPORTED_ROLES = ["china", "think", "neko_catgirl", "succubus", "isla"]

# =============================================================================
# 配置驱动架构：厂商定义 + 模型定义
# =============================================================================

@dataclass
class ProviderConfig:
    """厂商级默认配置，包括端点、鉴权变量和协议能力。"""
    name: str
    base_url: str
    api_key_env: str
    default_headers: Optional[Dict[str, str]] = None
    # 协议循环选择器（单字段）：该厂商默认走哪套 agentic 循环。
    # "openai_compat"   （OpenAI 兼容 Chat Completions，缺省）-> _agentic_loop_openai_compat
    # "gemini_native"   （Gemini 原生 API 流式桥接）          -> _agentic_loop_gemini_native
    # "anthropic_native"（Anthropic 原生 Messages）           -> _agentic_loop_anthropic
    # 旧值 "gemini_openai_compat"（OpenAI 兼容层非流式循环）已随 v2.6
    # Gemini 原生流式改造移除；未识别的标签回落主流 OpenAI 兼容循环。
    # 历史注：早期版本曾是 use_dedicated_loop(bool) + kind 两字段（kind 仅在
    # bool=True 时生效）。单字段化后"不声明 = OpenAI 兼容"，消除了
    # "kind 已声明但开关忘开"的半开状态——该状态下 api_client 按 kind 建
    # 原生客户端、循环层按 bool&&kind 路由进兼容循环，运行期会错配崩溃。
    dedicated_loop_kind: str = "openai_compat"
    # 是否支持 Prompt Caching（仅部分厂商需要显式标记）
    supports_prompt_cache: bool = False
    # 视觉输入是否需要"公开可访问 HTTP URL"而非 data:image/...;base64,... 内联格式。
    # 部分 OpenAI 兼容网关（如 Agnes）官方文档明确只接受 image_url 中的公开 URL，
    # 内联 base64 会被静默忽略甚至报 4xx。开启后，会在 _resolve_multimodal_content
    # 里优先用 R2 公开 URL（不泄露 Telegram bot token），R2 不可用时回退 base64。
    vision_prefer_url: bool = False
    # 是否向该网关下发"会话亲和键"（session_id / X-Session-Id，同一对话
    # 窗口/同一任务共用，清空对话时轮换，见 state.get_llm_session_key）。
    # 背景：OpenRouter 官方支持 body.session_id 粘性路由（见
    # agentic_loops._openrouter_extra_body）；而 Agnes 这类聚合网关
    # （Cloudflare -> new-api -> LiteLLM -> 多上游推理副本）按请求随机分发、
    # 各副本的前缀缓存互相隔离，实测同一逐字节稳定前缀的连续请求命中率在
    # 0%~100% 间随机波动（命中完全取决于落在哪个副本）。开启后会在每个
    # 请求上附带会话亲和键：网关若支持任一形式的 session 路由即可从第一
    # 个请求起粘住同一副本，让 agentic loop 的后续轮次直接命中上一轮写入
    # 的前缀缓存；不支持时未知字段/请求头会被网关安全忽略，零副作用。
    session_affinity: bool = False


@dataclass
class ModelConfig:
    """单个模型的能力、推理参数以及可选端点覆盖。

    能力字段未显式指定时继承厂商默认值；端点字段未显式指定时继承
    ``PROVIDERS[provider]``。
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
    # reasoning_effort:   努力档位 "none"/"low"/"medium"/"high"/"xhigh"/"max"
    #                     （部分模型支持，会透传）
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

    # ===================== 端点覆盖（每模型独立中转/协议）=====================
    # 背景：中转/聚合端点常见"同一 base_url 下不同模型协议不同"（如某端点
    # 的 OpenAI 兼容模型走 /v1/chat/completions，Anthropic 系模型走原生
    # Messages API，二者 502/404 互不兼容），或者"想用的模型分散在多个
    # 中转站"。原先端点信息完全挂在 provider 级（PROVIDERS[provider]），
    # 同一 provider 下所有模型被迫共用同一 base_url/key/协议，选完供应商
    # 还要再确认这台端点这个模型走不走得通，配置心智负担很重。
    #
    # 以下字段全部可选，None = 沿用 provider（PROVIDERS[provider]）的默认值；
    # 非 None = 仅对本模型生效的覆盖值，不影响同 provider 下的其它模型。
    # 端点覆盖改三件事："连到哪、用哪个 key、带什么请求头"，以及协议循环
    # 本身（dedicated_loop_kind，单字段选择器，None=继承厂商默认）。换句话说：
    #   - 想换端点但协议不变（同样是 OpenAI 兼容 / 同样是 Anthropic 原生）：
    #     只填 base_url / api_key_env（可选 default_headers）。
    #   - 想强制该模型走某种协议循环（如某中转的这个模型只认 Anthropic
    #     原生 Messages 协议，即使 provider 挂在 openrouter 之类壳下）：
    #     填 dedicated_loop_kind="anthropic_native"。
    #   - 反向需求（provider 默认走原生、某模型想回 OpenAI 兼容）：
    #     显式填 dedicated_loop_kind="openai_compat" 覆盖。
    # 见 get_effective_endpoint() 获取合并后的有效端点配置。
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    default_headers: Optional[Dict[str, str]] = None
    # 协议循环选择器（单字段，取值域同 ProviderConfig.dedicated_loop_kind）：
    # None = 继承厂商默认；显式声明即覆盖，无独立开关字段。
    dedicated_loop_kind: Optional[str] = None
    session_affinity: Optional[bool] = None
    vision_prefer_url: Optional[bool] = None

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
        # base_url 仅供 subagent 的一次性非流式补全调用继续使用
        # （subagent_tool._create_chat_completion 的 OpenAI 兼容客户端）；
        # 主 Agent Loop 已切换为原生 API 流式桥接（ai/gemini_bridge.py，
        # streamGenerateContent?alt=sse + 原生 function calling），
        # 不再经过该兼容端点。
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        # Gemini 使用原生流式专用循环（单字段协议选择器）
        dedicated_loop_kind="gemini_native",
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
        # 聚合网关多副本缓存隔离：不下发会话亲和键时，同一前缀的命中率随
        # 路由到的副本随机波动（生产日志中 run 边界 40%、run 内 90%+ 的
        # 交替即此原因）。开启后每个请求携带 session_id + X-Session-Id，
        # 网关支持时粘住同一副本；不支持时被忽略，无副作用。
        session_affinity=True,
    ),
    "anthropic": ProviderConfig(
        name="Anthropic",
        # base_url 仅为占位（保持 ProviderConfig 结构一致），api_client 对
        # anthropic 厂商不会用它构造 AsyncOpenAI 客户端，而是构造原生
        # AsyncAnthropic 客户端（见 api_client.py）。
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        # Anthropic 原生 Messages 协议专用循环（单字段协议选择器）
        dedicated_loop_kind="anthropic_native",
        # Anthropic 原生 prompt caching（cache_control 显式断点），由
        # _agentic_loop_anthropic 按 supports_prompt_cache 开启。
        supports_prompt_cache=True,
    ),
    "xxtf": ProviderConfig(
        default_headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
        name="XXTF",
        # 壳的默认端点按 OpenAI 协议填（gpt-5.6-sol 沿用这个默认值）；
        # AsyncOpenAI 会自动拼接为 {base_url}/chat/completions
        # -> https://xxtf.baby/v1/chat/completions。
        base_url="https://xxtf.baby/v1",
        api_key_env="XXTF_API_KEY",
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
        "max_output_tokens": 65536,
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
        "max_output_tokens": 65536,
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
        "max_output_tokens": 65536,
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
        "max_output_tokens": 65536,
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
        "max_output_tokens": 65536,
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
        "max_output_tokens": 65536,
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
        "max_output_tokens": 65536,
        "max_context": 128000,
    },
    "anthropic": {
        "vision": True,
        "audio": False,
        "video": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": True,
        "native_video": False,
        "supports_sampling": True,
        "supports_prompt_cache": True,
        "temperature": None,          # None -> 不发送，走供应商默认
        "top_p": None,                # None -> 不发送，走供应商默认
        "reasoning_enabled": None,    # None -> 不发送推理控制参数
        "reasoning_effort": None,
        "reasoning_max_tokens": None,
        "max_output_tokens": 65536,
        "max_context": 200000,
    },
    "xxtf": {
        "vision": True,
        "audio": False,
        "video": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": True,
        "native_video": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "temperature": None,          # None -> 不发送，走供应商默认
        "top_p": None,                # None -> 不发送，走供应商默认
        "reasoning_enabled": None,    # None -> 不发送推理控制参数
        "reasoning_effort": None,
        "reasoning_max_tokens": None,
        "max_output_tokens": 65536,
        "max_context": 200000,
    },
}


@dataclass
class EffectiveEndpoint:
    """某个模型合并后的最终端点配置。"""
    provider: str                 # 协议族标签（决定走哪套 agentic 循环 / 请求体形状）
    name: str                     # 展示名（沿用 provider 名，端点覆盖不改展示名）
    base_url: str
    api_key_env: str
    default_headers: Dict[str, str]
    dedicated_loop_kind: str
    supports_prompt_cache: bool
    vision_prefer_url: bool
    session_affinity: bool
    # 是否存在模型级端点覆盖（仅用于日志/调试，不参与业务判断）。
    is_override: bool = False


def get_effective_endpoint(model_info) -> EffectiveEndpoint:
    """
    返回某个 ModelConfig 实际应使用的端点配置：
    以 PROVIDERS[model_info.provider] 为默认值，逐字段用模型上非 None 的
    覆盖字段（base_url / api_key_env / default_headers / dedicated_loop_kind /
    session_affinity / vision_prefer_url）替换。

    这是"每模型独立配置中转端点/协议"的唯一合并出口：api_client.py /
    agentic_loops.py / attachment_content.py 等一切需要知道"这个模型到底
    连哪、用哪个 key、走什么协议循环"的地方，都应改为调用本函数，而不是
    直接 PROVIDERS.get(model_info.provider)。
    """
    provider_key = getattr(model_info, "provider", None)
    base = PROVIDERS.get(provider_key)
    if base is None:
        raise ValueError(f"未知厂商: {provider_key!r}，请检查 config.py 中的 PROVIDERS")

    def _pick(attr_name: str):
        override = getattr(model_info, attr_name, None)
        return override if override is not None else getattr(base, attr_name)

    override_base_url = getattr(model_info, "base_url", None)
    override_api_key_env = getattr(model_info, "api_key_env", None)
    override_headers = getattr(model_info, "default_headers", None)
    override_loop_kind = getattr(model_info, "dedicated_loop_kind", None)
    override_session_aff = getattr(model_info, "session_affinity", None)
    override_vision_url = getattr(model_info, "vision_prefer_url", None)

    is_override = any(
        v is not None
        for v in (
            override_base_url, override_api_key_env, override_headers,
            override_loop_kind, override_session_aff,
            override_vision_url,
        )
    )

    return EffectiveEndpoint(
        provider=provider_key,
        name=base.name,
        base_url=_pick("base_url"),
        api_key_env=_pick("api_key_env"),
        default_headers=(override_headers if override_headers is not None else (base.default_headers or {})),
        dedicated_loop_kind=_pick("dedicated_loop_kind"),
        supports_prompt_cache=bool(getattr(model_info, "supports_prompt_cache", base.supports_prompt_cache)),
        vision_prefer_url=bool(_pick("vision_prefer_url")),
        session_affinity=bool(_pick("session_affinity")),
        is_override=is_override,
    )


def _merge_with_defaults(provider: str, overrides: dict) -> dict:
    """将非 None 的模型字段覆盖到厂商默认能力上。"""
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


# 端点覆盖字段：这些字段不参与 _PROVIDER_DEFAULTS 的能力合并（vision/
# supports_tools 等走厂商默认继承的逻辑），而是"模型有填就用模型的，
# 模型没填就是 None（=沿用 provider）"，直接原样落到 ModelConfig 上，
# 由 get_effective_endpoint() 在使用时合并，因此这里先从 kwargs 中摘出、
# 不参与 _merge_with_defaults。
_ENDPOINT_OVERRIDE_FIELDS = (
    "base_url",
    "api_key_env",
    "default_headers",
    "dedicated_loop_kind",
    "session_affinity",
    "vision_prefer_url",
)


def make_model_config(
    model_id: str,
    provider: str,
    name: str,
    **kwargs
) -> ModelConfig:
    """
    工厂函数：根据 provider 和覆盖项创建 ModelConfig。

    除了原有的能力字段（vision/supports_tools/reasoning_* 等，走厂商
    默认继承），还接受端点覆盖字段（见 _ENDPOINT_OVERRIDE_FIELDS）：
    当某个中转端点对不同模型使用不同协议或不同子端点时，可以在具体
    模型这里单独指定，无需为此新建一个 provider。

    示例：假设 provider="my_relay" 的中转站里，
    gpt-5.6-sol 走 OpenAI 兼容协议、claude-opus-5 走 Anthropic 原生协议，
    但用的是同一个 base_url 与同一个 key：

        PROVIDERS["my_relay"] = ProviderConfig(
            name="MyRelay",
            base_url="https://xxtf.baby/query/v1",   # OpenAI 兼容子路径
            api_key_env="MY_RELAY_API_KEY",
        )

        SUPPORTED_MODELS["gpt-5.6-sol"] = make_model_config(
            model_id="gpt-5.6-sol",
            provider="my_relay",
            name="GPT 5.6 Sol",
        )
        SUPPORTED_MODELS["claude-opus-5"] = make_model_config(
            provider="my_relay",
            name="Claude Opus 5 (中转)",
            # 仅此模型覆盖：换协议 + 换子路径，key 仍沿用 my_relay 默认。
            dedicated_loop_kind="anthropic_native",
            base_url="https://xxtf.baby",
        )
    """
    endpoint_overrides = {
        field: kwargs.pop(field) for field in _ENDPOINT_OVERRIDE_FIELDS if field in kwargs
    }

    # 迁移守卫：协议循环已单字段化（dedicated_loop_kind），旧的两字段写法
    # 直接报错暴露，避免旧配置被静默吞掉后行为与预期不符。
    if "use_dedicated_loop" in kwargs:
        raise ValueError(
            f"模型 {model_id} 传入了已移除的字段 use_dedicated_loop："
            "协议循环已单字段化，请改用 dedicated_loop_kind"
            f"（合法值: {sorted(_VALID_DEDICATED_LOOP_KINDS)}，不填=继承厂商默认）。"
        )

    override_loop_kind = endpoint_overrides.get("dedicated_loop_kind")
    if override_loop_kind is not None and override_loop_kind not in _VALID_DEDICATED_LOOP_KINDS:
        raise ValueError(
            f"模型 {model_id} 的 dedicated_loop_kind={override_loop_kind!r} 无效，"
            f"合法值: {sorted(_VALID_DEDICATED_LOOP_KINDS)}"
        )
    override_api_key_env = endpoint_overrides.get("api_key_env")
    if override_api_key_env is not None and not hasattr(sys.modules[__name__], override_api_key_env):
        # 提前暴露拼写错误：真正的 key 变量必须在本模块顶部用
        # os.getenv(...) 定义好，否则运行期 api_client._get_api_key
        # 只会得到 None 并报"缺少 API Key"，定位起来更绕。
        raise ValueError(
            f"模型 {model_id} 的 api_key_env={override_api_key_env!r} 在 config.py 中未定义，"
            f"请先在文件顶部加一行 {override_api_key_env} = os.getenv({override_api_key_env!r}, \"\")"
        )

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
        base_url=endpoint_overrides.get("base_url"),
        api_key_env=endpoint_overrides.get("api_key_env"),
        default_headers=endpoint_overrides.get("default_headers"),
        dedicated_loop_kind=endpoint_overrides.get("dedicated_loop_kind"),
        session_affinity=endpoint_overrides.get("session_affinity"),
        vision_prefer_url=endpoint_overrides.get("vision_prefer_url"),
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
                     thinkingBudget=0 关闭思考）。该形状仍供 subagent 的
                     OpenAI 兼容客户端使用；主循环的原生流式桥接
                     （gemini_bridge._gemini_thinking_config）在同一出口上
                     解码为原生 generationConfig.thinkingConfig
                     （thinkingLevel / thinkingBudget / includeThoughts）。
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

    if provider == "anthropic":
        # Anthropic 原生 Messages API：thinking 是顶层字段
        # {"type": "enabled"/"disabled", "budget_tokens": int}（enabled 时
        # budget_tokens 必填，未显式配置预算时给一个保守默认值）。
        # 该分支仅供 _agentic_loop_anthropic 自行解读使用（该循环不经过
        # chat.completions.create/extra_body，是直接读取 SUPPORTED_MODELS
        # 字段，这里返回值主要供调用方一致性参考，不影响其它厂商）。
        if enabled is False:
            return ({}, {"thinking": {"type": "disabled"}})
        if enabled is True or budget is not None:
            return ({}, {"thinking": {"type": "enabled", "budget_tokens": int(budget) if budget else 10000}})
        return _REASONING_NOOP

    # 其他 OpenAI 兼容厂商：只透传 effort（能力不明的网关不发未知字段）。
    if effort is not None:
        return ({"reasoning_effort": effort}, {})
    return _REASONING_NOOP


# =============================================================================
# 模型列表（所有支持的模型）
# =============================================================================
SUPPORTED_MODELS: Dict[str, ModelConfig] = {}

# -----------------------------------------------------------------------------
# OpenRouter 模型
# -----------------------------------------------------------------------------
SUPPORTED_MODELS["z-ai/glm-5.2:free"] = make_model_config(
    model_id="z-ai/glm-5.2:free",
    provider="openrouter",
    name="Glm 5.2 Free",
    reasoning_enabled=True,
    reasoning_effort="high",
    max_context=256000,
    # 预览模型能力未完全确认，不发送推理控制和采样参数，走供应商默认。
)
SUPPORTED_MODELS["minimax/minimax-m3:free"] = make_model_config(
    model_id="minimax/minimax-m3:free",
    provider="openrouter",
    name="Minimax M3 Free",
    vision=True,
    video=True,
    reasoning_enabled=True,
    reasoning_effort="high",
    max_context=1000000,
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

# -----------------------------------------------------------------------------
# Agnes 免费模型
# -----------------------------------------------------------------------------
# (duplicate gemma entry removed)
SUPPORTED_MODELS["agnes-2.5-flash"] = make_model_config(
    model_id="agnes-2.5-flash",
    provider="agnes",
    name="Agnes 2.5 Flash",
    reasoning_enabled=True,
    reasoning_effort="high",
    max_context=512000,
    vision=True,
    # 默认主力模型：网关对推理/采样参数的支持未公开，保守起见两者都不发送，
    # 完全走供应商默认。确认网关支持后可在此显式开启。
)

# -----------------------------------------------------------------------------
# ModelScope 免费模型
# -----------------------------------------------------------------------------
SUPPORTED_MODELS["deepseek-ai/DeepSeek-V4-Flash-0731"] = make_model_config(
    model_id="deepseek-ai/DeepSeek-V4-Flash-0731",
    provider="modelscope",
    name="Deepseek V4 Flash",
    max_context=1000000,
    reasoning_enabled=True,
    reasoning_effort="high",
    temperature=0.6,
)

SUPPORTED_MODELS["ZhipuAI/GLM-5.2"] = make_model_config(
    model_id="ZhipuAI/GLM-5.2",
    provider="modelscope",
    name="GLM 5.2",
    max_context=1000000,
    reasoning_effort="high",
    # GLM 混合思考：ModelScope 通道开启思考，智谱官方建议思考模式
    # temperature=0.6；如需关闭改 reasoning_enabled=False。
    reasoning_enabled=True,
    temperature=0.6,
)

# -----------------------------------------------------------------------------
# Gemini 系列
# -----------------------------------------------------------------------------
SUPPORTED_MODELS["gemini-3.5-flash-lite"] = make_model_config(
    model_id="gemini-3.5-flash-lite",
    provider="gemini",
    name="Gemini 3.5 Flash-Lite",
    vision=True,
    max_context=1000000,
    # Flash-Lite 定位轻快：思考限制在低档，避免响应变慢。
    # Google 建议不调整 temperature（默认 1.0）；top_p 不配置即不发送。
    # 如需精确控制可改用 reasoning_max_tokens（映射 thinkingBudget）。
    reasoning_enabled=True,
    reasoning_effort="high",
    temperature=1.0,
)
# -----------------------------------------------------------------------------
# GLM 系列
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# 图像生成模型
# -----------------------------------------------------------------------------
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
SUPPORTED_MODELS["gpt-image-2"] = make_model_config(
    model_id="gpt-image-2",
    provider="xxtf",
    name="GPT Image 2 (XXTF)",
    native_image=True,
    max_context=32768,
    max_output_tokens=4000,
)

# -----------------------------------------------------------------------------
# 视频生成模型
# -----------------------------------------------------------------------------
SUPPORTED_MODELS["agnes-video-v2.0"] = make_model_config(
    model_id="agnes-video-v2.0",
    provider="agnes",
    name="Agnes Video V2.0",
    native_video=True,
    max_context=32768,
    max_output_tokens=4000,
)

# -----------------------------------------------------------------------------
# Anthropic 官方模型（原生 Messages API，专用循环）
# -----------------------------------------------------------------------------
# =============================================================================
# XXTF 中转（https://xxtf.baby）：同一模型名在该平台上有多种协议挂载方式，
# 这里按"平台标注的协议"接入，而不是按模型名猜协议——
#   claude-opus-5   平台标 anthropic -> 走 Anthropic 原生 Messages 协议
#   gpt-5.6-sol     平台标 openai    -> 走 OpenAI 协议（但入口是 /v1/responses，
#                                       项目目前只有 Chat Completions 循环，
#                                       见下方模型定义处的风险说明）
#
# 两个模型共用同一个 provider="xxtf" 壳、同一份 XXTF_API_KEY，但各自按
# 端点覆盖字段（base_url / dedicated_loop_kind）
# 分别连到 Anthropic 原生入口和 OpenAI 兼容入口，互不干扰
# （api_client.py 按 model_id 分别缓存客户端，见 APIClient.get_client_for_model）。
# =============================================================================
SUPPORTED_MODELS["claude-opus-5"] = make_model_config(
    supports_tools=True,
    model_id="claude-opus-5",
    provider="xxtf",
    name="Claude Opus 5 (XXTF)",
    vision=True,
    native_document=True,
    supports_prompt_cache=True,
    # 平台标注协议为 anthropic：走 Anthropic 原生 Messages 专用循环。
    # 注意 base_url 不带 /v1——AsyncAnthropic SDK 会自动拼接
    # {base_url}/v1/messages -> https://xxtf.baby/v1/messages，
    # 与项目截图中 claude-opus-5 / anthropic 协议那一行的入口一致。
    dedicated_loop_kind="anthropic_native",
    base_url="https://xxtf.baby",
    reasoning_enabled=True,
    max_context=1000000,
    reasoning_effort="Max",
    temperature=1.0,
)

SUPPORTED_MODELS["gpt-5.6-sol"] = make_model_config(
    model_id="gpt-5.6-sol",
    provider="xxtf",
    name="GPT 5.6 Sol (XXTF)",
    vision=True,
    max_context=1000000,
    reasoning_enabled=True,
    reasoning_effort="max",
    supports_tools=True,
    # 【已知风险，未验证】平台协议入口标注为 /v1/responses（OpenAI 新的
    # Responses API），与本项目现有 OpenAI 兼容循环使用的 Chat
    # Completions 协议（/v1/chat/completions）不是同一套协议——字段
    # 形状、流式事件、工具调用格式均不同，项目目前没有 Responses API
    # 专用循环。这里先按 Chat Completions 协议接入（不覆盖 base_url，
    # 沿用 PROVIDERS["xxtf"] 默认的 https://xxtf.baby/v1，实际会请求
    # https://xxtf.baby/v1/chat/completions），如果该中转的
    # /v1/responses 入口不接受 chat.completions 请求体/不在这个路径
    # 提供服务，请求会直接报错（404 或 400），届时需要为 Responses API
    # 单独实现一套专用循环（类似 anthropic_bridge.py / gemini_bridge.py
    # 的边界转换模式）才能真正打通。
)
# =============================================================================


# ========== 默认模型 ==========
DEFAULT_MODEL = "agnes-2.5-flash"
assert DEFAULT_MODEL in SUPPORTED_MODELS, f"默认模型 {DEFAULT_MODEL} 未定义"


# =============================================================================
# 白名单管理
# -----------------------------------------------------------------------------
# 存储模型（R2 权威 + 本地缓存）：
#   - R2 对象（默认 key: config/whitelist.txt，可用
#     APITELEGRAMCHAT_WHITELIST_R2_KEY 覆盖）是唯一权威数据源。
#     R2 PutObject 允许对同 key 覆盖写（"编辑"该文件），因此每次
#     /adduser、/deluser 修改后都把全量白名单重新推送到 R2。
#   - 本地文件（WHITELIST_FILE）只是缓存 / 离线回退：R2 网络故障时
#     保证权限判断不中断。
#   - 启动/部署时 load_whitelist() 优先从 R2 拉取全量白名单（把运维
#     直接编辑过的 R2 文件批量加载进白名单）；R2 未配置或拉取失败时
#     回退本地文件；R2 上没有对象但本地有数据时，把本地数据作为种子
#     推送上 R2（完成本地 → R2 的首次迁移）。
# 管理员保护：
#   ADMIN_USERS 是管理员名单，与"用户白名单"完全独立。管理员：
#   - 不能被 /adduser 加进用户白名单（add_whitelist_user 返回 admin）；
#   - 不能被 /deluser 从用户白名单删除（remove_whitelist_user 返回
#     admin——管理员根本不属于用户白名单，且显式拒绝而非提示不存在）；
#   - 加载本地文件 / R2 内容时会过滤掉管理员条目（防止手改数据绕过）。
#   管理员的授权走 is_admin_identity，不依赖白名单。
# 大小写语义：
#   Telegram 用户名大小写不敏感（@Alice 与 alice 是同一账号），因此
#   用户名条目统一归一化为小写存储与比较；纯数字 user_id 按精确字符串
#   比较。这保证 /adduser @Alice 后，实际用户名为 alice 的用户能通过
#   权限校验，不会出现"加了大写、小写进不来"的隐藏 bug。
# =============================================================================

WHITELIST_FILE = os.getenv("APITELEGRAMCHAT_WHITELIST_FILE") or "whitelist.txt"
# R2 白名单对象 key。R2 是扁平对象命名空间，key 里的 "/" 只是前缀约定；
# 但本地缓存回退会把 key 映射为磁盘路径，因此拒绝 ".." 段防路径穿越。
_raw_whitelist_r2_key = (os.getenv("APITELEGRAMCHAT_WHITELIST_R2_KEY") or "config/whitelist.txt").strip().strip("/")
if not _raw_whitelist_r2_key or any(part == ".." for part in _raw_whitelist_r2_key.split("/")):
    logger.warning("APITELEGRAMCHAT_WHITELIST_R2_KEY 不安全（%r），回退默认 config/whitelist.txt", _raw_whitelist_r2_key)
    _raw_whitelist_r2_key = "config/whitelist.txt"
WHITELIST_R2_KEY = _raw_whitelist_r2_key
del _raw_whitelist_r2_key
WHITELIST_CONTENT_TYPE = "text/plain; charset=utf-8"

ADMIN_USERS = ["dearella"]
WHITELIST_USERS = set()
_whitelist_lock = asyncio.Lock()


def _normalize_target(target: str) -> str:
    """归一化白名单/管理员条目：去空白、去 @ 前缀；用户名转小写，纯数字 ID 保持原样。"""
    t = str(target or "").strip().lstrip("@")
    if not t:
        return ""
    if t.isdigit():
        return t
    return t.lower()


def _build_admin_sets() -> tuple[set[str], set[str]]:
    """把 ADMIN_USERS 拆成（用户名小写集合, 数字ID集合），供大小写不敏感匹配。"""
    names: set[str] = set()
    ids: set[str] = set()
    for admin in ADMIN_USERS:
        a = _normalize_target(admin)
        if not a:
            continue
        if a.isdigit():
            ids.add(a)
        else:
            names.add(a)
    return names, ids


_ADMIN_NAME_SET, _ADMIN_ID_SET = _build_admin_sets()


def _is_admin_target(target: str) -> bool:
    """target（用户名或数字 ID）是否指向管理员。用户名比较大小写不敏感。"""
    t = _normalize_target(target)
    if not t:
        return False
    if t.isdigit():
        return t in _ADMIN_ID_SET
    return t in _ADMIN_NAME_SET


def is_admin_identity(username: str = "", user_id: str = "") -> bool:
    """按 Telegram 身份（用户名 / 数字 ID）判断是否管理员。

    app.is_admin 委托本函数，保证整个代码库的管理员判断语义一致：
    用户名大小写不敏感，数字 ID 精确匹配。
    """
    uid = str(user_id or "").strip()
    if uid and uid in _ADMIN_ID_SET:
        return True
    if username:
        u = _normalize_target(username)
        if u and u in _ADMIN_NAME_SET:
            return True
    return False


def is_whitelisted_identity(username: str = "", user_id: str = "") -> bool:
    """按 Telegram 身份（用户名 / 数字 ID）判断是否在用户白名单内。

    与白名单存储同源归一化：用户名小写比较，数字 ID 精确比较。
    注意：本函数只查"用户白名单"；管理员授权由 is_admin_identity 负责
    （app.is_authorized 先查管理员再查白名单）。
    """
    uid = str(user_id or "").strip()
    if uid and uid in WHITELIST_USERS:
        return True
    if username:
        u = _normalize_target(username)
        if u and u in WHITELIST_USERS:
            return True
    return False


def _parse_whitelist_bytes(data: bytes) -> set[str]:
    """解析白名单文件内容（UTF-8，容忍 BOM / CRLF），归一化并过滤管理员条目。

    防御性过滤：即使有人手改 R2 / 本地文件把管理员塞进用户白名单，
    加载时也会剔除——管理员权限走 is_admin_identity，与白名单无关。
    含内部空白（空格/制表符等）的条目不可能是合法的用户名或 ID，直接丢弃。
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    entries: set[str] = set()
    for raw in text.splitlines():
        entry = _normalize_target(raw)
        if not entry:
            continue
        if any(ch.isspace() for ch in entry):
            logger.warning("白名单包含非法条目（含空白字符），已忽略：%r", raw)
            continue
        if _is_admin_target(entry):
            logger.warning("白名单中发现管理员条目 %r，已忽略（管理员不进入用户白名单）", entry)
            continue
        entries.add(entry)
    return entries


def _resolve_whitelist_path() -> str:
    """返回白名单文件路径，优先使用绝对路径，否则挂到 data_root 下。"""
    if os.path.isabs(WHITELIST_FILE):
        return WHITELIST_FILE
    try:
        from apitelegramchat.workspace_paths import data_root
        return str(data_root() / WHITELIST_FILE)
    except Exception:
        logger.warning("_resolve_whitelist_path 失败，使用回退路径", exc_info=True)
        # 回退到环境变量指定的数据目录
        data_dir = os.getenv("APITELEGRAMCHAT_DATA_DIR", "/tmp/apitelegramchat_data")
        return os.path.join(data_dir, WHITELIST_FILE)


def _serialize_whitelist_bytes() -> bytes:
    """把当前内存白名单序列化为文件字节（排序后每行一个，UTF-8）。"""
    return ("".join(user + "\n" for user in sorted(WHITELIST_USERS))).encode("utf-8")


def _save_whitelist_unlocked() -> None:
    """在已持有 _whitelist_lock 的前提下把 WHITELIST_USERS 写入本地缓存文件。

    先写临时文件再原子 replace：进程在写入中途崩溃也不会留下残缺的
    白名单文件（残缺文件会在下次启动回退时变成"部分用户丢失"）。
    """
    path = _resolve_whitelist_path()
    try:
        from pathlib import Path
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(user + "\n" for user in sorted(WHITELIST_USERS))
        os.replace(tmp_path, path)
    except OSError:
        logger.warning("save_whitelist failed: %s", path, exc_info=True)


async def _push_whitelist_to_r2_unlocked() -> bool:
    """在已持有 _whitelist_lock 的前提下，把当前白名单全量推送到 R2。

    - R2 未配置：upload_bytes_to_r2 自动落到本地 r2_cache 镜像并返回
      file:// URL，语义上等同"同步成功"（本地模式没有远端可推）。
    - 推送失败（网络/权限/超时）：返回 False 交给调用方决定是否提示
      管理员。本地文件已经写入，此后任何一次成功推送都是全量内容，
      会自动把之前没同步成功的变更一并补齐（自愈）。
    延迟导入 s3_utils：s3_utils 顶层反向依赖本模块的 R2_* 常量，模块
    顶层导入会构成循环导入。
    """
    try:
        from apitelegramchat.s3_utils import upload_bytes_to_r2
        url = await upload_bytes_to_r2(
            _serialize_whitelist_bytes(),
            WHITELIST_R2_KEY,
            WHITELIST_CONTENT_TYPE,
        )
        if url is None:
            logger.warning("白名单推送 R2 失败（key=%s）", WHITELIST_R2_KEY)
            return False
        return True
    except Exception:
        logger.warning("白名单推送 R2 异常（key=%s）", WHITELIST_R2_KEY, exc_info=True)
        return False


async def load_whitelist():
    """启动/部署时加载白名单：R2 优先（权威），本地文件回退（缓存）。

    全程持有 _whitelist_lock，与增删操作互斥；内存 set 原地更新（clear
    + update），保证 "from config import WHITELIST_USERS" 拿到的引用
    始终指向同一个 set 对象、永远能看到最新内容。
    """
    async with _whitelist_lock:
        r2_configured = await _r2_configured_async()
        remote_data: bytes | None = None
        if r2_configured:
            try:
                from apitelegramchat.s3_utils import download_from_r2
                remote_data = await download_from_r2(WHITELIST_R2_KEY)
            except Exception:
                logger.warning("白名单 R2 拉取异常，将回退本地文件（key=%s）", WHITELIST_R2_KEY, exc_info=True)

        if remote_data is not None:
            loaded = _parse_whitelist_bytes(remote_data)
            logger.info("白名单已从 R2 加载：%d 个用户（key=%s）", len(loaded), WHITELIST_R2_KEY)
            WHITELIST_USERS.clear()
            WHITELIST_USERS.update(loaded)
            # 回写本地缓存文件：R2 是权威，本地缓存必须与之一致，
            # 供下一次 R2 故障时离线回退。
            _save_whitelist_unlocked()
            return

        # R2 未配置 / 对象不存在 / 网络故障：本地文件回退（本地模式下
        # whitelist.txt 就是直接数据源），绝不因 R2 故障清空白名单。
        try:
            with open(_resolve_whitelist_path(), "rb") as f:
                loaded = _parse_whitelist_bytes(f.read())
            WHITELIST_USERS.clear()
            WHITELIST_USERS.update(loaded)
            logger.info("白名单已从本地文件加载：%d 个用户（%s）", len(loaded), _resolve_whitelist_path())
        except FileNotFoundError:
            WHITELIST_USERS.clear()
        except OSError:
            # 读失败时保留当前内存状态（可能是热重载场景），不清空。
            logger.warning("load_whitelist failed: %s", _resolve_whitelist_path(), exc_info=True)

        # R2 已配置但对象不存在（首次部署 / 换 key）：把本地数据作为种子
        # 推送上 R2，完成本地 → R2 迁移。file_exists_in_r2 在网络故障时
        # 也返回 False，但那时播种上传同样会失败，不会误覆盖远端已有数据。
        if r2_configured and remote_data is None:
            try:
                from apitelegramchat.s3_utils import file_exists_in_r2
                if not await file_exists_in_r2(WHITELIST_R2_KEY) and WHITELIST_USERS:
                    pushed = await _push_whitelist_to_r2_unlocked()
                    logger.info("白名单本地种子已推送 R2（key=%s）：%s", WHITELIST_R2_KEY, pushed)
            except Exception:
                logger.warning("白名单 R2 播种检查失败（key=%s）", WHITELIST_R2_KEY, exc_info=True)


async def _r2_configured_async() -> bool:
    """R2 是否已配置（延迟导入，避免循环依赖）。"""
    try:
        from apitelegramchat.s3_utils import is_r2_configured
        return bool(is_r2_configured())
    except Exception:
        return False


async def save_whitelist():
    """把当前白名单写入本地文件并推送 R2（增删函数已自带，一般无需手动调用）。"""
    async with _whitelist_lock:
        _save_whitelist_unlocked()
        await _push_whitelist_to_r2_unlocked()


# add/remove 状态常量：app.py 据此生成 Telegram 回复，测试据此断言。
ADD_ADDED = "added"                       # 确实新增
ADD_EXISTS = "exists"                     # 目标已在白名单中
ADD_ADMIN_REJECTED = "admin"              # 目标是管理员，拒绝加入用户白名单
ADD_SYNC_FAILED = "added_sync_failed"     # 已加入并写本地，但推送 R2 失败
REMOVE_REMOVED = "removed"                # 确实移除
REMOVE_MISSING = "missing"                # 目标不在白名单中
REMOVE_ADMIN_REJECTED = "admin"           # 目标是管理员，用户白名单无权删除
REMOVE_SYNC_FAILED = "removed_sync_failed"  # 已移除并写本地，但推送 R2 失败


async def add_whitelist_user(target: str) -> str:
    """原子地把 target 加入白名单：改内存 → 写本地文件 → 推送 R2。

    返回 ADD_* 状态字符串。"改内存集合 + 写本地 + 推 R2" 整体在
    _whitelist_lock 内完成，与 load_whitelist/save_whitelist 互斥：
    并发 /adduser 不会互相覆盖，R2 收到的推送顺序与操作顺序一致，
    权威存储不会出现旧状态覆盖新状态。
    """
    normalized = _normalize_target(target)
    if not normalized:
        # 空目标无意义。app 入口已拦截，这里防御性返回"无需操作"。
        return ADD_EXISTS
    if _is_admin_target(normalized):
        # 管理员不能加入用户白名单：这是用户白名单，不是管理员名单；
        # 管理员本来就始终拥有全部权限，加进来只会混淆权限模型。
        logger.info("拒绝把管理员 %r 加入用户白名单", normalized)
        return ADD_ADMIN_REJECTED
    async with _whitelist_lock:
        if normalized in WHITELIST_USERS:
            return ADD_EXISTS
        WHITELIST_USERS.add(normalized)
        _save_whitelist_unlocked()
        sync_ok = await _push_whitelist_to_r2_unlocked()
        return ADD_ADDED if sync_ok else ADD_SYNC_FAILED


async def remove_whitelist_user(target: str) -> str:
    """原子地把 target 从白名单移除：改内存 → 写本地文件 → 推送 R2。

    返回 REMOVE_* 状态字符串。管理员不可删除（管理员不属于用户白名单，
    且显式拒绝而不是伪装成"不存在"，让管理员明确知道权限边界）。
    """
    normalized = _normalize_target(target)
    if not normalized:
        return REMOVE_MISSING
    if _is_admin_target(normalized):
        logger.info("拒绝从用户白名单删除管理员 %r", normalized)
        return REMOVE_ADMIN_REJECTED
    async with _whitelist_lock:
        if normalized not in WHITELIST_USERS:
            return REMOVE_MISSING
        WHITELIST_USERS.discard(normalized)
        _save_whitelist_unlocked()
        sync_ok = await _push_whitelist_to_r2_unlocked()
        return REMOVE_REMOVED if sync_ok else REMOVE_SYNC_FAILED


async def snapshot_whitelist() -> list[str]:
    """加锁读取白名单快照（排序后的列表），避免与并发的增删操作交叉读到中间态。"""
    async with _whitelist_lock:
        return sorted(WHITELIST_USERS)

# -----------------------------------------------------------------------------
# 缓存 TTL
# -----------------------------------------------------------------------------
CACHE_TTL = _positive_int_env("CACHE_TTL", 300, 10)
SEARCH_CACHE_TTL = _positive_int_env("SEARCH_CACHE_TTL", 300, 10)
FETCH_CACHE_TTL = _positive_int_env("FETCH_CACHE_TTL", 3600, 10)

# -----------------------------------------------------------------------------
# S3 / R2 配置
# -----------------------------------------------------------------------------
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")
R2_REGION = os.getenv("R2_REGION", "auto")

# -----------------------------------------------------------------------------
# 流式刷新阈值
# -----------------------------------------------------------------------------
# 草稿是用户感知 Agent 正在工作的唯一实时界面。默认值优先保证首字与
# 状态变更的可见性，同时仍低于 Telegram 草稿 API 的常规刷新频率。
STREAM_FLUSH_INTERVAL = _positive_float_env("STREAM_FLUSH_INTERVAL", 0.65, 0.25)
STREAM_SILENT_FORCE_FLUSH = _positive_float_env(
    "STREAM_SILENT_FORCE_FLUSH", 2.0, STREAM_FLUSH_INTERVAL
)

# -----------------------------------------------------------------------------
# 工具调用并发数
# -----------------------------------------------------------------------------
MAX_CONCURRENT_TOOLS = _positive_int_env("MAX_CONCURRENT_TOOLS", 16, 1)

# -----------------------------------------------------------------------------
# Telegram update 摄取通道（长轮询 / Webhook）
# -----------------------------------------------------------------------------
# INGEST_MODE 决定 update 从哪条链路进入 update_queue：
#
#   "polling"（默认，推荐）
#       应用主动 getUpdates 长轮询。update 内容走**出站响应体**，不经过
#       Render 边缘 Cloudflare WAF 的入站请求体检查，因此不会再出现
#       "某些文本内容的消息永久 403、Telegram 队头阻塞、全 bot 卡死"。
#
#   "webhook"
#       保持旧的 Telegram → POST /webhook 链路。仅在你已经把 WEBHOOK_URL
#       指向自建反向代理 / Cloudflare Worker（对请求体做过 base64 包装，
#       见 deploy/cloudflare-webhook-proxy.js）时才应使用。直连 Render 域名
#       时该模式存在已知的 WAF 误杀缺陷。
#
#   "auto"
#       配了 WEBHOOK_URL 就用 webhook，否则 polling（兼容旧部署的过渡值）。
_RAW_INGEST_MODE = (os.getenv("INGEST_MODE", "polling") or "polling").strip().lower()
if _RAW_INGEST_MODE not in {"polling", "webhook", "auto"}:
    logger.warning(
        "INGEST_MODE=%r 不是合法取值（polling/webhook/auto），已回退为 polling",
        _RAW_INGEST_MODE,
    )
    _RAW_INGEST_MODE = "polling"
if _RAW_INGEST_MODE == "auto":
    INGEST_MODE = "webhook" if _RAW_WEBHOOK_URL else "polling"
else:
    INGEST_MODE = _RAW_INGEST_MODE

# 单次 getUpdates 的服务端挂起时长（秒）。Telegram 建议 ≤50；25 与
# aiohttp 请求超时（下方 +15s 余量）配合，既省请求数又能快速感知断链。
TELEGRAM_POLL_TIMEOUT = _positive_int_env("TELEGRAM_POLL_TIMEOUT", 25, 1)
# 单次 getUpdates 最多取回多少条 update（Telegram 上限 100）。
TELEGRAM_POLL_LIMIT = min(_positive_int_env("TELEGRAM_POLL_LIMIT", 100, 1), 100)

# =============================================================================
# 安全补丁：读取后立即清洗敏感环境变量
# =============================================================================
_SENSITIVE_PATTERNS = (
    "TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD",
    "CREDENTIAL", "PRIVATE", "ACCESS", "WEBHOOK_TOKEN",
)
_SENSITIVE_EXACT = {
    "TELEGRAM_BOT_TOKEN", "GLM_API_KEY",
    "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY",
    "XAI_API_KEY", "GROQ_API_KEY", "MODELSCOPE_API_KEY", "AGNES_API_KEY",
    "XXTF_API_KEY",
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
        logger.info(
            "Scrubbed %d sensitive environment variables: %s",
            len(removed),
            ", ".join(removed),
        )

scrub_environment()
