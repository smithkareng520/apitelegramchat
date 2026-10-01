# search_engine.py —— 兼容 facade。
# 原 3997 行单体已按职责拆分至 search/ 包（caches / text_editor /
# tool_schemas / serper / fetch_url / quick_lookup / media_tools）。
# 本文件只 re-export 存在外部调用点的符号（经 AST 全仓引用分析精简）；
# 新代码请直接 import search.*。
#
# 注：地图工具族的本地包装层已移除 ——
# 高德能力由 gaode_mcp 服务器（mcp.json 注册）原生暴露，模型直接调用
# mcp__gaode_mcp__maps_*；见 tool_names.py 与 mcp_manager.py。
import logging

from search.tool_schemas import (  # noqa: F401
    build_deliver_reply_tool,
    build_media_tool_defs,
)
from search.serper import (  # noqa: F401
    execute_web_search,
)
from search.fetch_url import (  # noqa: F401
    execute_fetch_url,
)
from search.quick_lookup import (  # noqa: F401
    execute_exchange_rate,
    execute_weather,
    execute_wikipedia,
)
from search.media_tools import (  # noqa: F401
    execute_generate_image,
    execute_generate_video,
)
from search.text_editor import (  # noqa: F401
    execute_text_editor,
)

logger = logging.getLogger(__name__)
