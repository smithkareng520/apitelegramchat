import os
import asyncio

AUTHORIZED_USER = "dearella"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
XAI_API_KEY = os.getenv("XAI_API_KEY")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://apitelegramchat.onrender.com/webhook")
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

SUPPORTED_MODELS = {
    "anthropic/claude-3.7-sonnet:thinking": {
        "name": "claude-3.7-sonnet",
        "api_type": "openrouter",
        "vision": True,
        "document": True,
        "supports_search": False  # 不支持内置搜索
    },
    "perplexity/sonar-deep-research": {
        "name": "sonar-deep-research",
        "api_type": "openrouter",
        "vision": False,
        "document": False,
        "supports_search": True  # 不支持内置搜索
    },
    "meta-llama/llama-3.3-70b-instruct": {
        "name": "llama-3.3-70b",
        "api_type": "openrouter",
        "vision": False,
        "document": False,
        "supports_search": False  # 不支持内置搜索
    },
    "openai/gpt-4o-mini": {
        "name": "gpt-4o-mini",
        "api_type": "openrouter",
        "vision": True,
        "document": True,
        "supports_search": False  # 支持内置搜索
    },
    "mistralai/mistral-nemo": {
        "name": "mistral-nemo",
        "api_type": "openrouter",
        "vision": False,
        "document": False,
        "supports_search": False  # 不支持内置搜索
    },
    "qwen/qwen2.5-vl-32b-instruct:free": {
        "name": "qwen/qwen2.5-vl-32b",
        "api_type": "openrouter",
        "vision": False,
        "document": False,
        "supports_search": False
    },
    "deepseek/deepseek-chat-v3-0324:free": {
        "name": "deepseek-v3(openrouter)",
        "api_type": "openrouter",
        "vision": False,
        "document": False,
        "supports_search": False
    },
    "deepseek/deepseek-r1:free": {
        "name": "deepseek-r1(openrouter)",
        "api_type": "openrouter",
        "vision": False,
        "document": False,
        "supports_search": False
    },
    "gemini-2.0-flash": {
        "name": "gemini-2.0-flash",
        "api_type": "gemini",
        "vision": True,
        "document": False,
        "supports_search": False
    },
    "grok-2-vision-latest": {
        "name": "grok-2",
        "api_type": "grok",
        "vision": True,
        "document": False,
        "supports_search": False
    },
    "grok-2-image": {
        "name": "grok-2-image",
        "api_type": "grok",
        "vision": False,
        "document": False,
        "supports_search": False
    },
    "deepseek-reasoner": {
        "name": "DeepSeek-R1",
        "api_type": "deepseek",
        "vision": False,
        "document": False,
        "supports_search": False
    },
    "deepseek-chat": {
        "name": " DeepSeek-V3",
        "api_type": "deepseek",
        "vision": False,
        "document": False,
        "supports_search": False
    },
}

global_lock = asyncio.Lock()  # 定义全局异步锁
