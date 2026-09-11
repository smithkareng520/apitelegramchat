# search_engine.py —— 兼容 facade。
# 原 3997 行单体已按职责拆分至 search/ 包（caches / text_editor /
# tool_schemas / serper / fetch_url / quick_lookup / media_tools /
# map_tools）。本文件只 re-export 存在外部调用点的符号（经 AST 全仓
# 引用分析精简）；新代码请直接 import search.*。
import logging

from search.tool_schemas import (  # noqa: F401
    SEARCH_TOOLS,
    build_deliver_reply_tool,
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
from search.map_tools import (  # noqa: F401
    execute_distance,
    execute_geocode,
    execute_keyword_search,
    execute_nearby_search,
    execute_poi_details,
    execute_route,
)
from search.text_editor import (  # noqa: F401
    execute_text_editor,
)

logger = logging.getLogger(__name__)
