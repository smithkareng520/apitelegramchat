# tool_executors.py —— 兼容 facade。
# 原 2882 行单体已按职责拆分：tool_ui_render（卡片渲染）/ bash_session
# （持久 bash 沙箱）/ tool_result_format（结果→UI 分发）/ file_delivery
# （文件发送）/ tool_dispatch（统一调度）。本文件显式 re-export 全部
# 既有顶层符号，保证所有 `from tool_executors import X` 零改动。
# 新代码请直接 import 对应子模块。
import logging

from tool_dispatch import (  # noqa: F401
    LOCATION_LOOKUP_TOOLS,
    TOOL_RESPONSE_TOKEN_BUDGET,
    _TOOL_TIMEOUT_MARKER,
    _truncate_tool_result,
    dispatch_tool_call,
    execute_deliver_reply,
    tool_semaphore,
)
from tool_result_format import (  # noqa: F401
    _TOOL_TIMEOUT_LABELS,
    format_tool_result,
)
from tool_ui_render import (  # noqa: F401
    _ANSI_ESCAPE_RE,
    _PRE_BLOCK_MAX_CHARS,
    _SENSITIVE_RESULT_KEYS,
    _TOOL_UI_MAX_LINE_CHARS,
    _TOOL_UI_MAX_LINES,
    _UI_MAX_FIELDS,
    _UI_TAIL_LINES,
    _UI_VALUE_TOKEN_BUDGET,
    _compact_json,
    _display_key,
    _editor_result_summary,
    _escape_code_text,
    _extract_bash_command_from_envelope,
    _format_image_generation_result,
    _find_poi_records,
    _int_value,
    _list_of_dicts,
    _looks_like_http_url,
    _numbered_text,
    _parse_bash_envelope,
    _parse_structured_payload,
    _poi_photo_url,
    _poi_value,
    _render_bash_result,
    _render_code_panel,
    _render_code_text,
    _render_distance_card,
    _render_editor_quote,
    _render_editor_result,
    _render_map_location_card,
    _render_map_payload,
    _render_map_route_card,
    _render_media_failure_result,
    _render_poi_cards,
    _render_route_path,
    _render_structured_payload,
    _render_structured_value,
    _render_transit_plan,
    _strip_ansi,
    _tail_text_lines,
    _trim_ui_value,
    _truncate_ui_lines,
    _truncate_ui_lines_head_tail,
    extract_domain,
)
from bash_session import (  # noqa: F401
    SANDBOX_OUTPUT_MAX_CHARS,
    BashSession,
    BashSessionManager,
    _BashOutputBuffer,
    _RUNTIME_STATE_FILENAME,
    _bash_manager,
    _format_bash_envelope,
    _prepare_runtime_once,
    _runtime_state_path,
    _tool_version,
    execute_bash,
)
from file_delivery import (  # noqa: F401
    _REMOVED_TOOL_HINTS,
    execute_present_files,
)

logger = logging.getLogger(__name__)
