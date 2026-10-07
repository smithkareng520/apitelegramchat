# tool_executors.py —— 兼容 facade。
# （持久 bash 沙箱）/ tool_result_format（结果→UI 分发）/ file_delivery
# （文件发送）/ tool_dispatch（统一调度）。
# gaode_mcp 由 mcp_manager 直连；bash / text_editor / todo / memory 经 MCP
# stdio 服务器执行（mcpserver/server.py --module ...），不在 host 进程内分发。
# 这里只保留外部调用点引用的符号。
import logging

from tool_dispatch import (  # noqa: F401
    _TOOL_TIMEOUT_MARKER,
    _truncate_tool_result,
    dispatch_tool_call,
    execute_deliver_reply,
    tool_semaphore,
)
from tool_result_format import (  # noqa: F401
    format_tool_result,
)
# bash 会话管理器：app 关停时清理持久沙箱进程（唯一仍被 host 直接引用的
# bash 符号；bash 工具本体经 internal_bash MCP 服务器执行）。
from bash_session import _bash_manager  # noqa: F401

logger = logging.getLogger(__name__)
