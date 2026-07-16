# config.py
import os
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List

# ---------- 日志 ----------
logger = logging.getLogger(__name__)

# ---------- 环境变量 ----------
GEOAPIFY_KEY = os.getenv("GEOAPIFY_KEY", "")
AUTHORIZED_USER = "dearella"
IMGBB_KEY = os.getenv("IMGBB_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
XAI_API_KEY = os.getenv("XAI_API_KEY")
GOOGLE_CSE_KEY = os.getenv("GOOGLE_CSE_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY")
ORS_API_KEY = os.getenv("ORS_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GLM_API_KEY = os.getenv("GLM_API_KEY", "")
MODELSCOPE_API_KEY = os.getenv("MODELSCOPE_API_KEY", "")
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "")


# ---------- 运行模式 ----------
# telegram: 原有机器人模式（保持现状）
# mcp:      仅启动 MCP 服务器 / 工具生态，允许缺少 Telegram 相关环境变量
# 其它值也按 telegram 处理
APP_MODE = os.getenv("APP_MODE", "telegram").strip().lower()
SKIP_REQUIRED_ENV_CHECK = os.getenv("SKIP_REQUIRED_ENV_CHECK", "").strip().lower() in {"1", "true", "yes", "on"}


# ---------- DuckDuckGo 免费搜索 API（HTML 抓取回退已废弃）----------
# 通过环境变量配置 my-search-api 服务地址，避免反爬/封锁。
# 调用方式：<DDG_SEARCH_API_URL>?text=<quoted query>
# 返回 JSON，results[] 内每条至少包含 title / url / snippet。
DDG_SEARCH_API_URL = os.getenv("DDG_SEARCH_API_URL", "").strip()


WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")
_RAW_WEBHOOK_URL = os.getenv("WEBHOOK_URL") or ""
WEBHOOK_URL = f"{_RAW_WEBHOOK_URL}?token={WEBHOOK_TOKEN}" if _RAW_WEBHOOK_URL else ""

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

# ---------- 日志截断配置 ----------
LOG_TRUNCATE_LIMIT = int(os.getenv("LOG_TRUNCATE_LIMIT", "5000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# ---------- 必需环境变量检查 ----------
def _check_required_keys():
    # MCP 模式下允许缺省 Telegram / webhook 配置，
    # 这样同一份代码可以既作为 Telegram bot 运行，也可以作为独立 MCP 服务器运行。
    if APP_MODE == "mcp" or SKIP_REQUIRED_ENV_CHECK:
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
_check_required_keys()

# ---------- 全局锁（保留兼容） ----------
global_lock = asyncio.Lock()

# ---------- 角色相关 ----------
user_role_selections = {}
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
    supports_tools: Optional[bool] = None
    native_image: Optional[bool] = None
    native_document: Optional[bool] = None
    native_video: Optional[bool] = None
    supports_search: Optional[bool] = None
    supports_sampling: Optional[bool] = None
    supports_prompt_cache: Optional[bool] = None 
    max_output_tokens: Optional[int] = None
    max_context: Optional[int] = None  # <=== 【新增】最大上下文窗口

    @property
    def api_type(self) -> str:
        return self.provider

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)


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
    ),
}


# =============================================================================
# 厂商默认能力（模型未覆盖时使用）
# =============================================================================
_PROVIDER_DEFAULTS: Dict[str, Dict] = {
    "openrouter": {
        "vision": False,
        "audio": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "native_video": False,
        "supports_search": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "max_output_tokens": 8192,
        "max_context": 128000,
    },
    "modelscope": {
        "vision": False,
        "audio": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_search": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "max_output_tokens": 8192,
        "max_context": 128000,
    },
    "gemini": {
        "vision": True,
        "audio": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_search": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "max_output_tokens": 8192,
        "max_context": 1000000,
    },
    "grok": {
        "vision": False,
        "audio": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_search": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "max_output_tokens": 8192,
        "max_context": 128000,
    },
    "deepseek": {
        "vision": False,
        "audio": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_search": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "max_output_tokens": 8192,
        "max_context": 64000,
    },
    "glm": {
        "vision": False,
        "audio": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "supports_search": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "max_output_tokens": 8192,
        "max_context": 64000,
    },
    "agnes": {
        "vision": False,
        "audio": False,
        "supports_tools": True,
        "native_image": False,
        "native_document": False,
        "native_video": False,
        "supports_search": False,
        "supports_sampling": True,
        "supports_prompt_cache": False,
        "max_output_tokens": 8192,
        "max_context": 64000,
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


def make_model_config(
    model_id: str,
    provider: str,
    name: str,
    **kwargs
) -> ModelConfig:
    """工厂函数：根据 provider 和覆盖项创建 ModelConfig"""
    merged = _merge_with_defaults(provider, kwargs)

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
        supports_tools=merged.get("supports_tools"),
        native_image=merged.get("native_image"),
        native_document=merged.get("native_document"),
        native_video=merged.get("native_video"),
        supports_search=merged.get("supports_search"),
        supports_sampling=merged.get("supports_sampling"),
        supports_prompt_cache=merged.get("supports_prompt_cache"),
        max_output_tokens=merged.get("max_output_tokens", 8192),
        max_context=merged.get("max_context", 128000),
    )


# =============================================================================
# 模型列表（所有支持的模型）
# =============================================================================
SUPPORTED_MODELS: Dict[str, ModelConfig] = {}

# ---------- OpenRouter 免费模型 ----------
SUPPORTED_MODELS["nvidia/nemotron-3-ultra-550b-a55b:free"] = make_model_config(
    model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
    provider="openrouter",
    name="Nemotron 3 Ultra 550B A55B Free",
    supports_tools=True,
    max_context=1000000,
    max_output_tokens=66000,
)
SUPPORTED_MODELS["anthropic/claude-sonnet-5"] = make_model_config(
    model_id="anthropic/claude-sonnet-5",
    provider="openrouter",
    name="Claude Sonnet 5",
    supports_tools=True,
    vision=True,
    native_document=True,
    supports_prompt_cache=True,
    max_context=1000000,
    max_output_tokens=128000,
)
SUPPORTED_MODELS["poolside/laguna-m.1:free"] = make_model_config(
    model_id="poolside/laguna-m.1:free",
    provider="openrouter",
    name="Laguna m.1 Free",
    max_context=262000,
    max_output_tokens=33000,
)
SUPPORTED_MODELS["cohere/north-mini-code:free"] = make_model_config(
    model_id="cohere/north-mini-code:free",
    provider="openrouter",
    name="North Mini Code Free",
    max_context=256000,
    max_output_tokens=64000,
)
SUPPORTED_MODELS["tencent/hy3:free"] = make_model_config(
    model_id="tencent/hy3:free",
    provider="openrouter",
    name="Hy3 Free",
    max_context=262000,
    max_output_tokens=64000,
)
SUPPORTED_MODELS["google/gemma-4-31b-it:free"] = make_model_config(
    model_id="google/gemma-4-31b-it:free",
    provider="openrouter",
    name="Gemma 4 31B it",
    supports_tools=True,
    vision=True,
    max_context=262000,
    max_output_tokens=8000,
)

# ---------- Agnes 免费模型 ----------
# (duplicate gemma entry removed)
SUPPORTED_MODELS["agnes-2.0-flash"] = make_model_config(
    model_id="agnes-2.0-flash",
    provider="agnes",
    name="Agnes 2.0 Flash",
    max_context=256000,
    max_output_tokens=64000,
    vision=True,
)

# ---------- ModelScope 免费模型 ----------
SUPPORTED_MODELS["deepseek-ai/DeepSeek-V4-Pro"] = make_model_config(
    model_id="deepseek-ai/DeepSeek-V4-Pro",
    provider="modelscope",
    name="Deepseek V4 Pro",
    max_context=1000000,
    max_output_tokens=65536,
)

# ---------- Gemini 系列 ----------
SUPPORTED_MODELS["gemini-3.5-flash"] = make_model_config(
    model_id="gemini-3.5-flash",
    provider="gemini",
    name="Gemini 3.5 Flash",
    vision=True,
    max_context=1000000,
    max_output_tokens=65535,
)
# ---------- GLM 系列 ----------
# (Removed duplicate google/gemma-4-31b-it:free entry that overwrote the earlier one)
SUPPORTED_MODELS["glm-4.6v"] = make_model_config(
    model_id="glm-4.6v",
    provider="glm",
    name="GLM 4.6V",
    vision=True,
    max_context=128000,
    max_output_tokens=32000,
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
SUPPORTED_MODELS["fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA"] = make_model_config(
    model_id="fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA",
    provider="modelscope",
    name="Qwen Image Edit 2511 Multiple Angles LoRA",
    vision=True,
    native_image=True,
    max_context=32768,
    max_output_tokens=4000,
)
SUPPORTED_MODELS["Qwen/Qwen-Image-Edit-2511"] = make_model_config(
    model_id="Qwen/Qwen-Image-Edit-2511",
    provider="modelscope",
    name="Qwen Image Edit 2511",
    vision=True,
    native_image=True,
    max_context=32768,
    max_output_tokens=4000,
)
SUPPORTED_MODELS["MusePublic/489_ckpt_FLUX_1"] = make_model_config(
    model_id="MusePublic/489_ckpt_FLUX_1",
    provider="modelscope",
    name="FLUX.1 dev",
    native_image=True,
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
    max_output_tokens=33000,
)
SUPPORTED_MODELS["google/gemini-3-pro-image-preview"] = make_model_config(
    model_id="google/gemini-3-pro-image-preview",
    provider="openrouter",
    name="Gemini 3 Pro Image Preview",
    native_image=True,
    vision=True,
    supports_tools=False,
    max_context=66000,
    max_output_tokens=33000,
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
DEFAULT_MODEL = "tencent/hy3:free"
assert DEFAULT_MODEL in SUPPORTED_MODELS, f"默认模型 {DEFAULT_MODEL} 未定义"


# =============================================================================
# 自动发现模型（可选）：如果用户输入了未在 SUPPORTED_MODELS 中定义的模型，
# 但符合 {provider}/{model_id} 格式，可自动创建配置。
# =============================================================================
def discover_model(model_id: str) -> Optional[ModelConfig]:
    """根据模型 ID 自动发现配置（如果厂商支持）"""
    for provider in PROVIDERS:
        if model_id.startswith(provider + "/"):
            # 从厂商默认值创建
            defaults = _PROVIDER_DEFAULTS.get(provider, {})
            name = model_id.split("/")[-1]
            return ModelConfig(
                model_id=model_id,
                provider=provider,
                name=name,
                vision=defaults.get("vision"),
                audio=defaults.get("audio"),
                supports_tools=defaults.get("supports_tools"),
                native_image=defaults.get("native_image"),
                native_document=defaults.get("native_document"),
                native_video=defaults.get("native_video"),
                supports_search=defaults.get("supports_search"),
                supports_sampling=defaults.get("supports_sampling"),
                supports_prompt_cache=defaults.get("supports_prompt_cache"),
                max_output_tokens=defaults.get("max_output_tokens", 8192),
                max_context=defaults.get("max_context", 128000),
            )
    return None


def get_model_config(model_id: str) -> ModelConfig:
    """获取模型配置，优先从 SUPPORTED_MODELS，否则尝试自动发现"""
    if model_id in SUPPORTED_MODELS:
        return SUPPORTED_MODELS[model_id]
    discovered = discover_model(model_id)
    if discovered:
        return discovered
    # 降级：使用默认模型
    logger.warning(f"未知模型 {model_id}，降级到 {DEFAULT_MODEL}")
    return SUPPORTED_MODELS[DEFAULT_MODEL]


# =============================================================================
# 白名单管理
# =============================================================================
WHITELIST_FILE = "whitelist.txt"
ADMIN_USERS = ["dearella"]
WHITELIST_USERS = set()

def load_whitelist():
    global WHITELIST_USERS
    try:
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            WHITELIST_USERS = {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        WHITELIST_USERS = set()  # 修复了原代码中的拼写错误 WHILIST_USERS

def save_whitelist():
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        for user in sorted(WHITELIST_USERS):
            f.write(user + "\n")

# ---------- 缓存 TTL ----------
CACHE_TTL = 300
SEARCH_CACHE_TTL = 300
FETCH_CACHE_TTL = 3600

# ---------- S3 / R2 配置 ----------
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL")
R2_REGION = os.getenv("R2_REGION", "auto")

# ---------- 流式刷新阈值 ----------
STREAM_FLUSH_INTERVAL = 1.0
STREAM_FLUSH_CHARS = 200
STREAM_SILENT_FORCE_FLUSH = 4.0

# ---------- 工具调用并发数 ----------
MAX_CONCURRENT_TOOLS = 4

# ---------- 文件解析配置 ----------
PARSE_CONCURRENCY_LIMIT = int(os.getenv("PARSE_CONCURRENCY_LIMIT", "5"))
PARSE_TIMEOUT = int(os.getenv("PARSE_TIMEOUT", "60"))

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
    "GEOAPIFY_KEY", "IMGBB_KEY", "TOMTOM_API_KEY", "ORS_API_KEY",
    "GOOGLE_CSE_KEY", "GOOGLE_CSE_ID",
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
