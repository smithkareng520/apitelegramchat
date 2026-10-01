"""全局日志初始化与请求 ID 上下文（自 utils.py 拆出）。"""

import os
import sys
import logging
from logging import handlers as logging_handlers
import contextvars
from typing import Any

from config import LOG_LEVEL


# ---------- 配置日志 ----------
# 日志文件路径可由环境变量 LOG_FILE 覆盖；默认 /tmp/app.log 仅在可写时启用。
LOG_FILE = os.getenv("LOG_FILE", "/tmp/app.log")


class _MCPStreamableHTTPNoiseFilter(logging.Filter):
    """将 MCP SDK 的原始 ERROR traceback 降为一行 WARNING。

    ModelScope 网关偶发截断 JSON 响应体时，SDK 会以 ERROR + 完整
    traceback（40+ 行 httpx/httpcore 堆栈）记录 "Error parsing JSON
    response"。该异常客户端已通过「单次超时 → 分页定向重试 → 部分
    降级」处理，无需整页堆栈刷屏；降级为一行 WARNING 保留根因痕迹
    （字节计数等信息已由 search_engine 的降级日志补足）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # 故意不在这里调用 logger：本方法运行在 logging 过滤器管线内部，
            # 从 filter() 里再发一条日志有重入/递归风险（新日志记录会重新
            # 经过同一套 handler/filter 链）。保持静默放行是安全的选择。
            return True
        if record.levelno >= logging.ERROR and "Error parsing JSON response" in message:
            record.msg = message + "（上游网关截断响应体；已由超时+定向重试+降级处理）"
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True

def setup_logging() -> bool:
    """配置 root logger。返回 True 表示完成了配置，False 表示跳过。

    通过环境变量 APITELEGRAMCHAT_REQUIRE_LOGGING=1 可强制在导入时配置；
    默认情况下，若 root logger 已有 handler 则不再覆盖，便于宿主程序
    （如 unit tests、MCP server）自定义 logging config。
    """
    root_logger = logging.getLogger()
    # 应用 LOG_LEVEL 环境变量（默认 INFO）
    try:
        level = getattr(logging, LOG_LEVEL, logging.INFO)
    except Exception:
        # 注意：本函数在模块级可能于 `logger = logging.getLogger(__name__)`
        # （文件末尾）赋值之前就被调用（见文件底部的 import-time 触发），
        # 此处不能引用模块级 logger，否则会抛 NameError。
        level = logging.INFO
    if root_logger.level == logging.NOTSET or root_logger.level > level:
        root_logger.setLevel(level)
    logging.getLogger('botocore').setLevel(logging.WARNING)
    logging.getLogger('aiobotocore').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    # ModelScope MCP 网关会立即关闭 SSE GET 流，导致 SDK 客户端不停
    # 重连并每次打印一条 INFO（"GET stream disconnected, reconnecting
    # in 1000ms..."），大量冲刷日志。重连本身无害且自动进行，调高该
    # logger 级别降噪；响应体截断的原始 ERROR 则由
    # _MCPStreamableHTTPNoiseFilter 降级为一行 WARNING。
    sdk_logger = logging.getLogger('mcp.client.streamable_http')
    sdk_logger.setLevel(logging.WARNING)
    # 幂等安装：重复调用 setup_logging 不叠加 filter。
    if not any(isinstance(f, _MCPStreamableHTTPNoiseFilter) for f in sdk_logger.filters):
        sdk_logger.addFilter(_MCPStreamableHTTPNoiseFilter())

    # 仅在没有任何 handler 时才安装 console/file handler，
    # 避免重复 import（例如 utils 被 reload）造成 handler 累积和日志重复输出。
    if root_logger.handlers:
        return False

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(
        '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}'
    ))
    root_logger.addHandler(console_handler)

    try:
        file_handler = logging_handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10*1024*1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}'
        ))
        root_logger.addHandler(file_handler)
    except Exception as e:
        # 同上：此处不能用模块级 logger（可能尚未赋值），保留 print 到 stderr
        # 作为在 logger 就绪前也不会丢失的兜底，同时把详情打全（原来只有 e 的
        # str()，堆栈信息会丢失）。
        import traceback
        print(f"Warning: 无法创建文件日志 {LOG_FILE}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return True

# 仅在显式开启或 root logger 还没有 handler 时执行初始化；无条件覆盖
# root logger 会让 MCP server、tests 等宿主失去对自己 logging 配置的控制。
if os.getenv("APITELEGRAMCHAT_REQUIRE_LOGGING", "0") in {"1", "true", "yes", "on"} or not logging.getLogger().handlers:
    setup_logging()

logger = logging.getLogger(__name__)
# ---------- 请求ID上下文 ----------
# 使用 contextvars 替代全局 dict，避免并发协程间 request_id 互相覆盖
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="unknown")

def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)

def get_request_id() -> str:
    return _request_id_var.get()

class RequestIdAdapter(logging.LoggerAdapter):
    def process(self, msg: Any, kwargs: Any) -> tuple[str, Any]:
        rid = get_request_id()
        return f"[{rid}] {msg}", kwargs

def get_logger(name: str) -> RequestIdAdapter:
    return RequestIdAdapter(logging.getLogger(name), {})
