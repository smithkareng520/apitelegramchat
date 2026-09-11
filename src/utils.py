# utils.py —— 兼容 facade。
# 原 2301 行单体已按职责拆分至 core/ 包（logging / http / rich_media /
# telegram_messaging 等）。本文件只 re-export 存在外部调用点的符号
# （经 AST 全仓引用分析精简）；新代码请直接 import core.*。
import logging

from core.logging_setup import (  # noqa: F401
    get_logger,
    set_request_id,
    setup_logging,
)
from core.http_session import (  # noqa: F401
    close_http_session,
    get_http_session,
)
from core.chat_guard import (  # noqa: F401
    _notify_chat_unreachable,
)
from core.text_utils import (  # noqa: F401
    get_current_time,
)
from core.rich_media import (  # noqa: F401
    escape_media_url_attr,
    strip_html_tags,
)
from core.balances import (  # noqa: F401
    query_provider_balances,
)
from core.telegram_messaging import (  # noqa: F401
    RateLimitError,
    delete_message,
    delete_message_fast,
    is_draft_dead,
    mark_draft_dead,
    send_rich_html_message,
    send_rich_message_draft,
    send_chat_action,
)
from core.message_extract import (  # noqa: F401
    extract_message_text,
    extract_sticker_metadata,
    sticker_metadata_to_text,
    transcribe_audio_with_groq,
)

# 保持与拆分前一致的模块级 logger 名（"utils"）
logger = logging.getLogger(__name__)
