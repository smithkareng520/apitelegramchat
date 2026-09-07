# utils.py —— 兼容 facade。
# 原 2301 行单体已按职责拆分至 core/ 包（logging / http / rich_media /
# telegram_messaging 等）；本文件显式 re-export 全部既有顶层符号，
# 保证所有 `from utils import X` 调用点零改动。新代码请直接 import core.*。
import logging

from core.logging_setup import (  # noqa: F401
    LOG_FILE,
    RequestIdAdapter,
    _MCPStreamableHTTPNoiseFilter,
    _request_id_var,
    get_logger,
    get_request_id,
    set_request_id,
    setup_logging,
)
from core.http_session import (  # noqa: F401
    _http_session,
    _http_session_lock,
    close_http_session,
    get_http_session,
)
from core.chat_guard import (  # noqa: F401
    _permanent_chat_error_reason,
    _notify_chat_unreachable,
)
from core.text_utils import (  # noqa: F401
    _F,
    _SMART_AMP_PATTERN,
    escape_html,
    get_current_time,
    retry_async,
)
from core.rich_media import (  # noqa: F401
    _MEDIA_SRC_RE,
    _TG_SLIDESHOW_RE,
    _WATCH_PAGE_URL_PATTERNS,
    _build_demoted_anchor,
    _demote_all_media_to_links,
    _demote_specific_media_url,
    _demote_watch_page_videos,
    _escape_media_url_text,
    _extract_media_urls,
    _looks_like_watch_page,
    _media_url_domain,
    _rich_message_html_payload,
    _rich_message_plain_text_fallback,
    _selective_media_fallback,
    _slideshow_inner_has_media,
    _strip_invalid_media_urls,
    _unwrap_slideshow_inner,
    escape_media_url_attr,
    strip_html_tags,
)
from core.balances import (  # noqa: F401
    BalanceResult,
    _BALANCE_QUERYERS,
    _fetch_json,
    _query_deepseek_balance,
    _query_openrouter_balance,
    query_provider_balances,
)
from core.telegram_messaging import (  # noqa: F401
    RateLimitError,
    _DRAFT_MAX_ATTEMPTS,
    _DRAFT_MIN_INTERVAL,
    _DRAFT_REQUEST_TIMEOUT,
    _DRAFT_RETRY_DELAY,
    _DRAFT_CONNECT_TIMEOUT,
    _DraftSendState,
    _bump_draft_failure,
    _cleanup_dead_draft_state,
    _dead_draft_ids,
    _dead_draft_ids_lock,
    _draft_states,
    _draft_states_lock,
    _get_draft_send_lock,
    _get_draft_state,
    _is_current_active_draft,
    _peek_draft_state,
    _reassert_active_draft_content,
    _reset_draft_failure,
    _rich_html_contains_video,
    _VIDEO_TAG_RE,
    delete_message,
    delete_message_fast,
    is_draft_dead,
    mark_draft_dead,
    send_rich_html_message,
    send_rich_message_draft,
    send_chat_action,
    serialize_with_active_draft,
)
from core.message_extract import (  # noqa: F401
    _extract_rich_message_text,
    _rich_message_to_text,
    extract_message_text,
    extract_sticker_metadata,
    sticker_metadata_to_text,
    transcribe_audio_with_groq,
)

# 保持与拆分前一致的模块级 logger 名（"utils"）
logger = logging.getLogger(__name__)
