"""工具 schema 数据底座：SEARCH_TOOLS 与 message_user/deliver_reply（自 search_engine.py 拆出）。

含图像/视频模型目录（TEXT_ONLY_MODELS 等，由 SUPPORTED_MODELS
按能力推导）——SEARCH_TOOLS 内 generate_image/edit_image 的 enum
直接引用这些列表。
"""


from config import SUPPORTED_MODELS

from web_search_filter import (
    SEARCH_DEFAULT_RESULTS as _SEARCH_DEFAULT_RESULTS,
    SEARCH_MAX_RESULTS as _SEARCH_MAX_RESULTS,
)
from todo_tool import TODO_TOOL
from memory_tool import MEMORY_TOOL
try:
    from subagent_tool import SUBAGENT_TOOL
except Exception:  # pragma: no cover - optional dependency fallback
    SUBAGENT_TOOL = []  # type: ignore[assignment]

import logging

logger = logging.getLogger(__name__)


def _get_image_models_by_capability() -> tuple[list[str], list[str]]:
    """
    返回两个列表：
    - text_models: 支持文生图的全部模型（native_image=True；vision=True 的
      模型同样能纯文生图，一并列入，如 gpt-image-2 / gemini 图像模型）
    - edit_models: 支持图生图/编辑（native_image=True, vision=True）
    """
    text_models = []
    edit_models = []
    for model_id, cfg in SUPPORTED_MODELS.items():
        if not cfg.native_image:
            continue
        text_models.append(model_id)
        if cfg.vision:
            edit_models.append(model_id)
    return text_models, edit_models

# ----- 工具 1：纯文生图 -----
TEXT_ONLY_MODELS, EDIT_MODELS = _get_image_models_by_capability()


def _get_video_models() -> list[str]:
    """返回所有支持原生视频生成的模型 ID（native_video=True）。"""
    return [model_id for model_id, cfg in SUPPORTED_MODELS.items() if cfg.native_video]


# ----- 工具 2：视频生成 -----
VIDEO_MODELS = _get_video_models()

# ---------- 工具定义 ----------
# message_user（原 ask_user）：双用途人类交互工具。
# - 提问：带 options，出按钮卡等用户选；
# - 给用户发消息：不带 options，像给同学发一条消息——发送后等用户自由
#   回复；超时（默认 2 分钟）即"用户不在"（不是错误），已发送的消息
#   卡片会被简化成纯文本正文留在聊天记录里；用户回复了就是正常。
MESSAGE_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "message_user",
        "description": (
            "Send a message to the user and optionally wait for their reply. Two use cases: "
            "(a) ask a clarifying question — provide 2-6 concise options as buttons; "
            "(b) message the user — omit options entirely and the question is delivered "
            "as a plain text message, like texting a friend: you send it, wait briefly "
            "(default 2 minutes), and if there is no reply the user is simply away. Any "
            "text the user types next is returned as the reply. "
            "The tool suspends until the user answers in Telegram, the user cancels, or the "
            "timeout (default 2 minutes) elapses. A timeout returns {\"type\":\"expired\"} which "
            "simply means the user is currently away — it is NOT an error; wrap up the turn "
            "gracefully (after a timeout the sent message stays in the chat as plain text). "
            "In proactive/background turns this is also the natural channel to reach the user. "
            "Never call this tool more than once in the same tool-call batch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "消息正文（问题或通知内容）。清晰、具体；不要重复用户已明确提供的信息。"
                },
                "options": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 8,
                    "description": (
                        "可选的选项列表。提供时渲染为按钮提问卡；完全省略（或空数组）则为"
                        "给用户发消息模式（纯文本消息，像给朋友发一条消息），等待用户自由"
                        "文本回复。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "稳定的内部选项 ID。"},
                            "label": {"type": "string", "description": "按钮上显示的简短文字。"},
                            "description": {"type": "string", "description": "可选的补充说明。"}
                        },
                        "required": ["id", "label"]
                    }
                },
                "multiple": {
                    "type": "boolean",
                    "default": False,
                    "description": "是否允许多选（仅提问模式）。多选时用户需要点击提交。"
                },
                "allow_custom": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否允许用户放弃预设选项，直接输入自定义回答（仅提问模式）。"
                }
            },
            "required": ["question"]
        }
    }
}

# 向后兼容别名：旧代码 / 旧引用仍可导入 ASK_USER_TOOL。
ASK_USER_TOOL = MESSAGE_USER_TOOL

# deliver_reply：/show off（静默模式）下模型通过 send 布尔参数选择是否
# 把「本轮最后一条助手消息的 content 字段」通过 sendRichMessage 交付给用户。
# send=true：系统发送该正文（不经过草稿，也不含 reasoning 等其他字段）；
# send=false：显式不发送。send 的**缺省值（不填）按事件源区分**（见
# build_deliver_reply_tool）：静默 USER 回合（用户主动发消息）默认 true
# ——不填按发送处理，整轮不调用时收尾还会兜底发送最终回复，只有显式
# send=false 才保持静默；静默 TIMER 回合（后台巡检）默认 false——不填 /
# 不调用均不发送，必须显式填 true。上一轮交付或抑制与否不影响本轮，
# 缺省值由 get_ai_response 在每轮 agent 开始时重置。草稿开启（/show on）
# 时本工具不进入工具面，模型看不到也就不会调用，除了草稿外不会产生
# 单独 content；历史中的调用痕迹也会从出站上下文拔除（见
# tool_visibility.SILENT_ONLY_TOOLS）。
def build_deliver_reply_tool(default_send: bool = False) -> dict:
    """按本轮 send 缺省值生成 deliver_reply 工具定义。

    - ``default_send=True``（/show off + USER 回合）：send 不填默认发送，
      显式填 false 才静默（用户主动发消息默认应收到回复）；
    - ``default_send=False``（/show off + TIMER 回合，保持旧行为）：send
      不填 / false 均不发送，必须显式填 true 才交付。
    工具名不变（deliver_reply），描述与参数 default 随缺省值调整，供模型
    在当轮请求中读到正确的默认语义。
    """
    if default_send:
        send_param_desc = (
            "是否把本轮最后一条助手消息正文发送给用户：true 或不填（默认 true）"
            "=发送；显式填 false=本轮不发送、完全静默。"
        )
        default_clause = (
            "本回合 send=true 或不填（默认 true）都会发送；只有当你明确判断"
            "本轮内容不该发给用户时，才显式填 send=false——此后本轮完全静默，"
            "系统不再兜底发送，用户不会收到任何内容。另请注意：即使你整轮"
            "不调用本工具，回合结束时系统也会把本轮最后一条非空助手消息的"
            "正文本身经 sendRichMessage 兜底交付给用户（与本工具 send=true "
            "发送的内容完全同源）——因此中间轮次的过程性文字用户收不到，"
            "务必把完整、自包含的最终回复写成最后一条消息的正文。"
        )
    else:
        send_param_desc = (
            "是否把本轮最后一条助手消息正文发送给用户：true=发送；"
            "false 或不填=不发送（默认 false，TIMER 主动巡检回合默认保持静默）。"
        )
        default_clause = (
            "本回合是 TIMER 主动巡检回合：send=false 或不填（默认 false）"
            "均不发送——与\"不调用\"语义等价，本轮保持静默；需要用户看到"
            "结论时必须显式填 send=true。"
        )
    tool = {
        "type": "function",
        "function": {
            "name": "deliver_reply",
            "description": (
                "仅在草稿预览关闭（静默模式，/show off）时可用：通过 send 参数决定是否"
                "把你当前这条消息的正文（即本轮最后一条助手内容的 content 字段本身，"
                "不含其他内容）作为一条永久富文本消息（Telegram Rich HTML）通过 "
                "sendRichMessage 直接发送给用户，不经过草稿。send=true：发送——系统发送"
                "的就是你当前消息的正文本身，因此正确用法是先把完整、自包含的最终回复"
                "直接写成消息正文，再在同一条消息里调用本工具并填 send=true。"
                + default_clause
                + " 重要：在正文中用文字\"提到\"或\"声称已使用\"本工具不会产生任何"
                "效果——只有通过标准 tool_calls API 机制真正发起调用才会执行。静默模式下"
                "你的流式输出不会自动送达用户。交付成功后不要再调用本工具，也不要输出"
                "\"已发送/已确认\"之类的确认正文——用户已经收到，重复确认只会造成冗余"
                "消息。需要提问或留言可用 message_user（其超时表示用户不在，不是错误）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "send": {
                        "type": "boolean",
                        "description": send_param_desc,
                        "default": bool(default_send),
                    },
                },
                "required": []
            }
        }
    }
    return tool


# 向后兼容别名：等价于 TIMER 回合（默认 false）的工具定义。新代码请用
# build_deliver_reply_tool(default_send=...) 按事件源生成。
DELIVER_REPLY_TOOL = build_deliver_reply_tool(default_send=False)

SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search Google via Serper. One tool, four modes (controlled by `mode`): "
                "search (default, web pages), images (text-to-image), videos (text-to-video), "
                "lens (reverse image search — pass `image_url`). "
                "`mode` accepts a single value or an array of values to run multiple modes "
                "in one call (e.g. [\"search\",\"images\"]). "
                "For in-depth reading of a result, follow up with fetch_url (one URL per call)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：搜索2024年诺贝尔奖"
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。search/images/videos 模式必填；lens 模式可选（用作文字约束）。",
                    },
                    "mode": {
                        "type": ["string", "array"],
                        "items": {"type": "string", "enum": ["search", "images", "videos", "lens"]},
                        "description": "搜索模式：search（默认，网页）/ images（搜图）/ videos（搜视频）/ lens（以图搜图）。可传数组以一次性执行多个模式。",
                        "default": "search",
                    },
                    "image_url": {
                        "type": "string",
                        "description": "lens 模式必填：要反向搜索的图片 URL。其他模式忽略。",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": (
                            f"可选：单个 mode 的结果数上限。search: 1-{_SEARCH_MAX_RESULTS}（多页聚合）；"
                            f"images/videos/lens: 1-100。不填时默认 {_SEARCH_DEFAULT_RESULTS} 条。"
                        ),
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "可选：search 模式下的结果偏移量（向后翻页），从 0 开始；其他模式忽略。",
                        "minimum": 0,
                    },
                    "gl": {
                        "type": "string",
                        "description": "可选：地区码（如 us / cn / al）。不填取默认 cn。",
                    },
                    "hl": {
                        "type": "string",
                        "description": "可选：界面语言（如 en / zh-cn / ar）。不填取默认 zh-cn。",
                    },
                    "tbs": {
                        "type": "string",
                        "description": (
                            "可选：时间筛选。常用值：qdr:h（过去1小时）/ qdr:d（过去24小时）/ "
                            "qdr:w（过去一周）/ qdr:m（过去一月）/ qdr:y（过去一年）。不填不限时间。"
                        ),
                    },
                },
                "required": [],
                "anyOf": [
                    {"required": ["query"]},
                    {"required": ["image_url"]}
                ],
            },
            "input_examples": [
                {"query": "2024 诺贝尔物理学奖 获奖者", "num_results": 5},
                {"query": "Python 3.13 新特性", "num_results": 3},
                {"query": "React Hooks 教程", "num_results": 10, "offset": 10},
                {"query": "球球大作战 官网", "mode": "images", "num_results": 8},
                {"query": "苹果发布会", "mode": "videos", "num_results": 5, "tbs": "qdr:w"},
                {"image_url": "https://example.com/photo.jpg", "mode": "lens", "num_results": 10},
                {"query": "特斯拉 model y", "mode": ["search", "images", "videos"], "num_results": 5},
            ],
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch and read the full content of a specific URL. Returns the page rendered as Telegram Rich Message HTML "
                "that mirrors the original page structure and order: headings, paragraphs, lists, tables, quotes, "
                "code blocks, links and media (images, embedded videos, iframe players such as YouTube/Bilibili, "
                "audio) all appear at their original positions; image carousels are grouped into <tg-slideshow>. "
                "You may quote or reuse the relevant HTML fragments (including <img>/<video>/<a> tags with their "
                "original URLs) directly in your reply. "
                "Use when a search result needs deeper reading or the user gave you a link. One URL per call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整的 URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wikipedia",
            "description": (
                "Look up a topic on Wikipedia by keyword and return the full article rendered as "
                "Telegram Rich Message HTML mirroring the original page structure: headings, "
                "paragraphs, lists, tables (episode lists, statistics), images and links all appear "
                "at their original positions. The keyword is resolved to the best-matching page in "
                "one step (no separate search needed). You may quote or reuse the relevant HTML "
                "fragments (including <table>, <img>, <a> tags) directly in your reply. "
                "Prefer for encyclopedic / factual / definitional queries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "条目标题或关键词"},
                    "lang": {"type": "string", "description": "语言代码（zh/en）", "default": "zh"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "exchange_rate",
            "description": "Get real-time exchange rates for a base currency. Optionally filter to a single target currency.",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：查询美元兑人民币汇率"
                    },
                    "base": {"type": "string", "description": "基础货币代码（如 USD、CNY）"},
                    "target": {"type": "string", "description": "目标货币（可选）"}
                },
                "required": ["base"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_lookup",
            "description": "Look up book metadata (title, author, cover, rating, abstract) by title, author, or ISBN.",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：查找三体作者"
                    },
                    "query": {"type": "string", "description": "书名、作者或 ISBN"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": (
                "Get weather conditions and forecasts for a city. Returns current conditions, "
                "hourly forecast, and up to 5 days of daily forecast. "
                "Use for any weather-related question. unit='c' (default) returns Celsius, "
                "'f' returns Fahrenheit. The `hours` parameter (default 6, max 24) controls "
                "how many hourly entries are returned — pass a larger value when the user "
                "asks about the rest of the day or tomorrow morning."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：查询北京今日天气"
                    },
                    "city": {"type": "string", "description": "城市名（如 Beijing、Shanghai）"},
                    "unit": {"type": "string", "enum": ["c", "f"], "default": "c"},
                    "hours": {"type": "integer", "default": 6, "description": "返回的逐时预报条数（1-24，默认 6）。需要更长展望时传大值。"}
                },
                "required": ["city"]
            },
            "input_examples": [
                {"city": "Beijing", "unit": "c", "hours": 12},
                {"city": "New York", "unit": "f"}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "news",
            "description": "Get latest headlines from major news sources (bbc / reuters / cna / cnn / nytimes / guardian / zaobao / xinhua / all).",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["bbc", "reuters", "cna", "cnn", "nytimes", "guardian", "zaobao", "xinhua", "all"], "default": "bbc"},
                    "limit": {"type": "integer", "default": 5, "description": "返回条数（1-10）"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "crypto_price",
            "description": "Get the current spot price of a cryptocurrency (btc / eth / doge / etc.) in the requested currency.",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "description": "简述本次操作目的（≤60字）。示例：查询比特币价格"
                    },
                    "coin": {"type": "string", "description": "币种符号（btc、eth、doge 等）"},
                    "currency": {"type": "string", "default": "usd", "description": "计价货币"}
                },
                "required": ["coin"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "qr_code",
            "description": "Generate a QR code image from text or URL and return its public URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要编码的文本或 URL"}
                },
                "required": ["text"]
            }
        }
    },
    # ===================== 地图工具（全部由 amap-maps MCP 提供） =====================
    {
        "type": "function",
        "function": {
            "name": "geocode",
            "description": "将地址或地名转换为经纬度坐标（地理编码）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "address": {"type": "string", "description": "地址或地名，如“北京市海淀区中关村”。"}
                },
                "required": ["address"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "route",
            "description": "统一规划骑行、步行、驾车或公交路线。origin 与 destination 必须是高德坐标“经度,纬度”；公交跨城时必须同时提供 city 和 cityd。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "origin": {"type": "string", "description": "起点经纬度，格式为“经度,纬度”，例如“116.397128,39.916527”。"},
                    "destination": {"type": "string", "description": "终点经纬度，格式为“经度,纬度”。"},
                    "mode": {"type": "string", "enum": ["cycling", "walking", "driving", "transit"], "default": "driving", "description": "骑行、步行、驾车或公交。"},
                    "city": {"type": "string", "description": "公交起点城市；跨城公交时必填。"},
                    "cityd": {"type": "string", "description": "公交终点城市；跨城公交时必填。"}
                },
                "required": ["origin", "destination"]
            },
            "input_examples": [
                {"origin": "116.397128,39.916527", "destination": "116.481488,39.990464", "mode": "cycling"},
                {"origin": "116.397128,39.916527", "destination": "121.473701,31.230416", "mode": "transit", "city": "北京", "cityd": "上海"}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "distance",
            "description": "测量两个高德经纬度坐标之间的直线距离。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "origin": {"type": "string", "description": "起点经纬度，格式“经度,纬度”。"},
                    "destination": {"type": "string", "description": "终点经纬度，格式“经度,纬度”。"}
                },
                "required": ["origin", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "poi_keyword_search",
            "description": "按关键词搜索 POI；有明确城市范围时传 city，不要将 POI ID 传入本工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "keywords": {"type": "string", "description": "搜索关键词，如“故宫博物院”。"},
                    "city": {"type": "string", "description": "可选的查询城市，如“北京”。"}
                },
                "required": ["keywords"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "poi_nearby_search",
            "description": "在指定中心点附近搜索 POI。location 必须是“经度,纬度”，radius 单位为米。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "keywords": {"type": "string", "description": "搜索关键词，如“咖啡馆”。"},
                    "location": {"type": "string", "description": "中心点经纬度，格式“经度,纬度”。"},
                    "radius": {"type": "integer", "description": "半径，单位米，范围 1–50000，默认 1000。"}
                },
                "required": ["keywords", "location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "poi_details",
            "description": "根据关键词搜索或周边搜索返回的 POI ID 获取地点详情；不要传地点名称。",
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                    "id": {"type": "string", "description": "关键词搜索或周边搜索返回的 POI ID。"}
                },
                "required": ["id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "text_editor",
            "description": (
                "Safely view or edit UTF-8 text files and explore directories inside the workspace. "
                "The available commands are: view, str_replace, create, and insert. "
                "Always call view immediately before editing. "
                "view: displays file contents with 1-based line numbers (supports view_range=[start_line, end_line], where -1 means end of file). "
                "If path is a directory (or '.' for workspace root), view lists files and directories up to 2 levels deep. "
                "create: creates a new file with file_text (fails if file already exists). "
                "str_replace: replaces old_str with new_str. old_str must match exactly once in the file; if multiple matches occur, their line numbers are reported. "
                "insert: inserts insert_text (or new_str) after insert_line (1-based; use 0 to insert at the beginning). "
                "After edits, a snippet around the modified section is returned for immediate verification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": ["view", "str_replace", "create", "insert"],
                        "description": "The text-editor operation to perform: view, str_replace, create, or insert."
                    },
                    "path": {
                        "type": "string",
                        "description": "Path of a file or directory inside the workspace (e.g. 'src/main.py', '.' for root). Leading slashes and '/workspace/' prefixes are automatically normalized."
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "For view (files only): [start_line, end_line], 1-based; end_line=-1 reads to the end."
                    },
                    "old_str": {
                        "type": "string",
                        "description": "For str_replace: exact existing text. It must have exactly one match in the file."
                    },
                    "new_str": {
                        "type": "string",
                        "description": "For str_replace: replacement text. Also accepted as the text to insert for insert command."
                    },
                    "file_text": {
                        "type": "string",
                        "description": "For create: complete initial file content; may be empty."
                    },
                    "insert_line": {
                        "type": "integer",
                        "description": "For insert: insert after this 1-based line number; 0 inserts at the beginning."
                    },
                    "insert_text": {
                        "type": "string",
                        "description": "For insert: text to add after insert_line (alternatively use new_str)."
                    }
                },
                "required": ["command", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute bash commands inside the user's per-session workspace. "
                "IMPORTANT: every call MUST fill the _description parameter with one short "
                "sentence (≤60 chars, same language as the user) saying what this command is "
                "for — it is shown to the user as live execution progress; calls missing it "
                "will be rejected by argument validation. "
                "Use this tool for installs, tests, builds, running scripts, git operations, "
                "and inspecting workspace files.\n"
                "\n"
                "ENVIRONMENT & NETWORK (important — read once, saves you wasted calls):\n"
                "- Outbound network IS allowed. curl and wget are available (if the image lacks the "
                "real binary, an equivalent Python stdlib shim is installed automatically).\n"
                "- Toolchain already in the image: python3 (+pip), node/npm, gcc/g++, make, cmake, "
                "ccache, git, jq, zip/unzip, LibreOffice, pandoc, ImageMagick, poppler, tesseract.\n"
                "- Install extra Python packages with `pip install --user <pkg>` (cache persists). "
                "apt-get is NOT usable — the filesystem outside your workspace is read-only.\n"
                "- If a command genuinely returns `command not found`, do NOT retry it unchanged; "
                "substitute a python3 stdlib equivalent (urllib.request for HTTP, etc.) or use the "
                "fetch_url tool.\n"
                "- Very long output is preserved head+tail: if you see a truncation notice, the "
                "middle was omitted — redirect output to a file (`cmd > out.log`) and inspect it "
                "with grep/tail/text_editor when you need everything.\n"
                "\n"
                "WORKSPACE & WRITABLE SCOPE (read once — this saves 5+ wasted calls):\n"
                "- Your starting cwd IS the workspace root. Its absolute path is in the $WORKSPACE\n"
                "  env var (`echo $WORKSPACE`); every bash result also begins with a\n"
                "  terminal-style prompt line — `/abs/cwd$ <command>` — showing the\n"
                "  directory that command ran in, so you always know where you are\n"
                "  (it updates after `cd`, just like a real shell prompt).\n"
                "- A Landlock sandbox makes ONLY the workspace tree writable. ALL other paths\n"
                "  — /tmp, /home, /root, /workspace, / — are unwritable (most are unreadable\n"
                "  too): `curl -o` there fails with exit code 23, Python writes raise\n"
                "  PermissionError, `ls` may report Permission denied.\n"
                "- NEVER `cd` out of the workspace to download or create files (including the\n"
                "  habitual `cd /tmp`). Download straight into your cwd or a subdir:\n"
                "  `curl -LO <url>`, or `mkdir -p assets && curl -o assets/x.bin <url>`.\n"
                "- TMPDIR already points to a writable private cache inside the sandbox, so\n"
                "  mktemp / Python tempfile / build-tool temp files work unchanged.\n"
                "\n"
                "Avoid interactive or long-running programs (vim, top, less, watch, -it shells, "
                "daemons); they will block the session. If a command appears stuck, set restart=true "
                "to reset the session and retry with a non-interactive variant.\n"
                "\n"
                "UPLOAD & DOWNLOAD DIRECTORIES (inside your workspace root):\n"
                "- download/ holds files the user has sent you (uploaded documents etc.).\n"
                "  Read them directly from your cwd: `ls download/`, `cat download/brief.pdf`.\n"
                "- upload/ is the staging area for outgoing files. To send a file to the\n"
                "  user, copy it here first (`cp report.pdf upload/report.pdf`), then call\n"
                "  present_files with the workspace-relative path `upload/report.pdf`.\n"
                "- Both directories live at the root of your workspace (your starting cwd).\n"
                "  You MAY freely read and write files inside them via relative paths.\n"
                "- You MAY NOT `cd` into upload/ or download/, and you MAY NOT execute any\n"
                "  command while your cwd is inside either of them. The sandbox rejects\n"
                "  `cd upload/...` and any subsequent command; if a command is rejected,\n"
                "  `cd` back to your workdir first, then operate via relative paths.\n"
                "\n"
                "To read a skill's instructions, `cd skills/<skill_id>` from your cwd."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "_description": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "【必填】意图描述：用一句话说明本次命令的目的（≤60字，与用户语言一致），"
                            "会作为执行进度实时展示给用户。示例：查看项目文件列表 / "
                            "安装依赖并运行测试 / 读取用户上传的文档"
                        )
                    },
                    "command": {
                        "type": "string",
                        "description": "要执行的 bash 命令。"
                    },
                    "restart": {
                        "type": "boolean",
                        "description": "true 则重启 bash 会话（清空状态）。"
                    }
                },
                # command 是功能上的必填字段（没有命令的 bash 调用无意义）：
                # 显式声明后，L2 schema 校验层能把「缺 command」以可操作
                # 错误回传模型自纠，strict 规范化也会正确将其保持为
                # 非可空必填，而不是被当作可选字段。
                # _description 同样显式声明为必填：草稿消息（rich draft 的
                # 工具组/单工具块进行态摘要）依赖它展示命令意图；漏填时
                # L2 会拒绝并回传「补 _description」的可操作错误，模型一
                # 轮自纠即可；strict 模式下保持非可空 string，不会被模型
                # 用 null 糊弄过去（null 会在 strip_null_arguments 后变成
                # 缺键，同样被 L2 拦截）。
                "required": ["_description", "command"]
            },
            "input_examples": [
                {"_description": "查看项目文件列表", "command": "ls -la"},
                {"_description": "安装依赖并运行测试", "command": "pip install --user pytest && python3 -m pytest -q"},
                {"_description": "读取用户上传的文档", "command": "head -c 2000 download/brief.pdf | strings | head -40"},
                {"_description": "把报告放入发送暂存区", "command": "cp report.pdf upload/report.pdf"},
                {"_description": "重启卡死的会话", "restart": True}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "present_files",
            "description": (
                "Send one or more files from upload/ to the chat as attachments. Files MUST already be "
                "staged under upload/ via bash (e.g. `cp out.txt upload/out.txt`). Pass "
                "workspace-relative paths, including the `upload/` prefix (e.g. "
                "`upload/out.txt` or `upload/reports/report.pdf`). Absolute paths inside "
                "the per-chat workspace are also accepted. Wildcards are not supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要发送的文件路径列表（相对于 workspace 根目录；待发送文件必须位于 upload/ 下，例如 upload/report.pdf）。"
                    }
                },
                "required": ["paths"]
            }
        }
    },
    *(
        [{
            "type": "function",
            "function": {
                "name": "generate_image_from_text",
                "description": (
                    "Generate a new image from a text prompt only (no reference image). Use when the user wants to create an image from scratch. "
                    f"Available models: {', '.join(TEXT_ONLY_MODELS)}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                        "prompt": {"type": "string", "description": "详细的图片描述"},
                        "model": {
                            "type": "string",
                            "enum": TEXT_ONLY_MODELS,
                            "description": "选择一个支持文生图的模型。"
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
                            "default": "1:1"
                        },
                        "image_size": {
                            "type": "string",
                            "enum": ["1K", "2K", "4K"],
                            "default": "1K"
                        },
                        "num_images": {
                            "type": "integer",
                            "default": 1,
                            "minimum": 1,
                            "maximum": 4
                        }
                    },
                    "required": ["prompt", "model"]
                }
            }
        }]
        if TEXT_ONLY_MODELS else []
    ),
    *(
        [{
            "type": "function",
            "function": {
                "name": "edit_image_with_reference",
                "description": (
                    "Edit an existing image using a reference image + a text prompt. Use when the user provides an image and wants to change something (style, object, background, angle, etc.). "
                    f"Available models: {', '.join(EDIT_MODELS)}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                        "prompt": {"type": "string", "description": "编辑指令（如 '改成水彩画风格'）"},
                        "image_url": {
                            "type": "string",
                            "description": "参考图的 URL 或 base64 数据。用户上传过图片时必填。"
                        },
                        "model": {
                            "type": "string",
                            "enum": EDIT_MODELS,
                            "description": "选择一个支持图生图编辑的模型。"
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
                            "default": "1:1"
                        },
                        "image_size": {
                            "type": "string",
                            "enum": ["1K", "2K", "4K"],
                            "default": "1K"
                        },
                        "num_images": {
                            "type": "integer",
                            "default": 1,
                            "minimum": 1,
                            "maximum": 4
                        }
                    },
                    "required": ["prompt", "image_url", "model"]
                }
            }
        }]
        if EDIT_MODELS else []
    ),
    *(
        [{
            "type": "function",
            "function": {
                "name": "generate_video",
                "description": (
                    "Generate a short video from a text prompt. Use when the user explicitly asks to create / generate / make a video. Do NOT use for animated images or GIFs (use generate_image_from_text instead). Generation is async and may take 1-5 minutes. On success, it returns a stable HTTPS URL in the exact form `视频链接：https://...`, just like image-generation tools return image URLs. In your next final response, embed that exact URL as a separate rich-media block: <figure><video src=\"URL\"></video><figcaption>已生成视频</figcaption></figure>; never send only a bare URL or ordinary hyperlink. "
                    f"Available models: {', '.join(VIDEO_MODELS) if VIDEO_MODELS else '(none configured)'}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "_description": {"type": "string", "description": "简述本次操作目的（≤60字）。"},
                        "prompt": {
                            "type": "string",
                            "description": "视频场景详细描述（主体、运动、镜头、风格等）。"
                        },
                        "model": {
                            "type": "string",
                            "enum": VIDEO_MODELS,
                            "description": "选择一个支持文生视频的模型。"
                        },
                        "duration": {
                            "type": "integer",
                            "description": "视频时长（秒），范围 3-30，默认 5。",
                            "default": 5,
                            "minimum": 3,
                            "maximum": 30
                        }
                    },
                    "required": ["prompt", "model"]
                }
            }
        }]
        if VIDEO_MODELS else []
    ),
    ASK_USER_TOOL,  # message_user（已改名，见上方定义）
    # ===================== 任务 / 待办工具 =====================
    # 让 agent 拥有持久化的待办清单能力：add/list/done/undone/delete/clear/edit。
    # 数据按用户隔离，存放在 ./state/{user_id}/todos.json 并随 R2 同步。
    # 仅在工具结果区显示富文本摘要；交互由 message_user 工具统一处理。
    TODO_TOOL,
    # ===================== 长期记忆工具 =====================
    # 跨会话保留的事实/偏好/人物/事件——不同于会自动修剪的对话历史。
    # 数据落在 ./state/{user_id}/memories.json，随 R2 同步。
    MEMORY_TOOL,
    # ===================== 子 Agent 工具 =====================
    # 派生一个干净上下文的子 agent 处理子任务，自带最小 agentic loop，
    # 工具白名单受控，禁递归调用 subagent/memory。
    SUBAGENT_TOOL,
]


# =============================================================================
# 工具实现
# =============================================================================


