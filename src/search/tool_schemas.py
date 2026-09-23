"""工具 schema 数据底座：SEARCH_TOOLS 与 message_user/deliver_reply（自 search_engine.py 拆出）。

含图像/视频模型目录（TEXT_ONLY_MODELS / EDIT_MODELS /
GENERATE_ONLY_MODELS 等，由 SUPPORTED_MODELS 按能力推导）——
SEARCH_TOOLS 内统一图像工具 generate_image 的 model enum 与
"什么模型可编辑/仅能生成"的能力说明直接引用这些列表。
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
    返回两个列表（按模型配置能力推导，不按操作意图归类）：
    - text_models: 支持文生图的全部模型（image_output=True；image_input=True 的
      模型同样能纯文生图，一并列入，如 gpt-image-2 / gemini 图像模型）
    - edit_models: 生成+编辑双能力模型（image_output=True, image_input=True）。
      ⚠️ 这不是"只能编辑"的模型桶：该类模型在同一端点上，不带参考图即
      纯文生图、带参考图即编辑（如 agnes-image-2.5-flash 恒定 POST
      /images/generations，extra_body.image 可选——不传=生成，传=编辑）。
      生成/编辑是"每次调用"由 image_url 是否携带决定的操作语义，不是
      模型属性；模型属性只有"允不允许携带 image_url"这一条能力边界。
    """
    text_models = []
    edit_models = []
    for model_id, cfg in SUPPORTED_MODELS.items():
        if not cfg.image_output:
            continue
        text_models.append(model_id)
        if cfg.image_input:
            edit_models.append(model_id)
    return text_models, edit_models

# ----- 图像模型能力目录（统一图像工具 generate_image 用）-----
TEXT_ONLY_MODELS, EDIT_MODELS = _get_image_models_by_capability()

# 双能力模型显式别名（生成+编辑二合一，image_url 可选）。工具描述以
# "Dual-mode models"名义列出该类，避免把模型呈现成"非纯生成即纯编辑"
# 的两个互斥桶——EDIT_MODELS 里的模型同样能省略 image_url 纯文生图。
DUAL_MODE_MODELS = list(EDIT_MODELS)


def _get_video_models() -> list[str]:
    """返回所有支持原生视频生成的模型 ID（video_output=True）。"""
    return [model_id for model_id, cfg in SUPPORTED_MODELS.items() if cfg.video_output]


# ----- 视频生成模型目录 -----
VIDEO_MODELS = _get_video_models()

# 仅支持文生图（不可携带参考图编辑）的图像模型 = 全部图像模型 - 双能力模型。
# generate_image 的工具描述据此向模型说明能力边界：双能力模型 image_url
# 可选（省略=生成、提供=编辑）；仅生成模型不接受 image_url。
GENERATE_ONLY_MODELS = [m for m in TEXT_ONLY_MODELS if m not in EDIT_MODELS]

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
        "description": ("Send a message to the user and optionally wait for a reply. "
            "For a choice question, provide 2-6 options; for a plain message, omit `options`. "
            "A timeout means the user is away, not an error. Never call more than once in one batch."),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1, "description": "Message or question to send. Be clear and specific."
                },
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 6,
                    "description": (
                        "可选的选项列表。提供时渲染为按钮提问卡；完全省略（或空数组）则为"
                        "给用户发消息模式（纯文本消息，像给朋友发一条消息），等待用户自由"
                        "文本回复。"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "description": "Stable option id."},
                            "label": {"type": "string", "minLength": 1, "description": "Button label."},
                            "description": {"type": "string", "description": "Optional supporting text."}
                        },
                        "required": ["id", "label"],
                        "additionalProperties": False
                    }
                },
                "multiple": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow multiple selections. Only used with `options`."
                },
                "allow_custom": {
                    "type": "boolean",
                    "default": True,
                    "description": "Allow a free-form reply instead of an option."
                }
            },
            "required": ["question"],
            "additionalProperties": False
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
            "description": ("Deliver the current assistant message in silent mode. "
                "send=true sends the current message body; false suppresses delivery. "
                + default_clause
                + " Call only for the final answer; after success, do not call again or add a confirmation."),
            "parameters": {
                "type": "object",
                "properties": {
                    "send": {
                        "type": "boolean",
                        "description": send_param_desc,
                        "default": bool(default_send),
                    },
                },
                "required": [],
                "additionalProperties": False
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
                "Search the web, images, videos, or reverse-search an image. "
                "Set `mode` to one mode or a list of modes; use `image_url` for lens. "
                "Use `fetch_url` to read a specific result in depth."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Search query. Required for search/images/videos; optional for lens.",
                    },
                    "mode": {
                        "type": ["string", "array"],
                        "items": {
                            "type": "string",
                            "enum": ["search", "images", "videos", "lens"],
                        },
                        "minItems": 1,
                        "maxItems": 4,
                        "description": "Search mode(s). Default: search. A list runs multiple modes in one call.",
                        "default": "search",
                    },
                    "image_url": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Image URL for lens mode. Ignored by other modes.",
                    },
                    "num_results": {
                        "type": "integer",
                        "description": (
                            f"Maximum results per mode. search: 1-{_SEARCH_MAX_RESULTS}; "
                            f"other modes: 1-100. Default: {_SEARCH_DEFAULT_RESULTS}."
                        ),
                        "minimum": 1,
                        "maximum": 100,
                        "default": _SEARCH_DEFAULT_RESULTS,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Result offset for search mode; ignored by other modes.",
                        "minimum": 0,
                        "default": 0,
                    },
                    "gl": {
                        "type": "string",
                        "description": "Region code, e.g. `us` or `cn`. Default: `cn`.",
                        "default": "cn",
                    },
                    "hl": {
                        "type": "string",
                        "description": "Interface language, e.g. `en` or `zh-cn`. Default: `zh-cn`.",
                        "default": "zh-cn",
                    },
                    "tbs": {
                        "type": "string",
                        "description": "Time filter, e.g. `qdr:h`, `qdr:d`, `qdr:w`, `qdr:m`, or `qdr:y`.",
                    },
                },
                "required": [],
                "additionalProperties": False,
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
                "Fetch and read a specific URL. Use for a user-provided link or a search result in depth. "
                "One URL per call; rich HTML preserves page structure and embedded media."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1, "description": "Full URL, including scheme."}
                },
                "required": ["url"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "wikipedia",
            "description": (
                "Look up a topic on Wikipedia by keyword. Use for encyclopedic, factual, or definitional questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "description": "Page title or keyword."},
                    "lang": {
                        "type": "string",
                        "enum": ["zh", "en"],
                        "description": "Wikipedia language. Default: `zh`.",
                        "default": "zh",
                    }
                },
                "required": ["query"],
                "additionalProperties": False
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
                    "base": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 3,
                        "pattern": "^[A-Za-z]{3}$",
                        "description": "Base currency code, e.g. `USD`.",
                    },
                    "target": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 3,
                        "pattern": "^[A-Za-z]{3}$",
                        "description": "Optional target currency code, e.g. `CNY`.",
                    }
                },
                "required": ["base"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": (
                "Get current weather and forecasts for a city. `unit` controls temperature units; "
                "`hours` controls hourly forecast length."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "minLength": 1, "description": "City name."},
                    "unit": {
                        "type": "string",
                        "enum": ["c", "f"],
                        "description": "Temperature unit. Default: Celsius (`c`).",
                        "default": "c",
                    },
                    "hours": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 24,
                        "description": "Hourly forecast entries. Default: 6.",
                        "default": 6,
                    }
                },
                "required": ["city"],
                "additionalProperties": False
            },
            "input_examples": [
                {"city": "Beijing", "unit": "c", "hours": 12},
                {"city": "New York", "unit": "f"}
            ]
        }
    },
    # ===================== 地图工具（全部由 amap-maps MCP 提供） =====================
    {
        "type": "function",
        "function": {
            "name": "geocode",
            "description": "Convert an address or place name to coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {"type": "string", "minLength": 1, "description": "Address or place name."}
                },
                "required": ["address"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "route",
            "description": (
                "Plan a cycling, walking, driving, or transit route. "
                "`origin` and `destination` must be Gaode `longitude,latitude`; "
                "cross-city transit also needs `city` and `cityd`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "minLength": 1, "description": "Start coordinate as `longitude,latitude`."},
                    "destination": {"type": "string", "minLength": 1, "description": "End coordinate as `longitude,latitude`."},
                    "mode": {
                        "type": "string",
                        "enum": ["cycling", "walking", "driving", "transit"],
                        "description": "Route mode. Default: `driving`.",
                        "default": "driving",
                    },
                    "city": {"type": "string", "description": "Transit origin city for cross-city transit."},
                    "cityd": {"type": "string", "description": "Transit destination city for cross-city transit."}
                },
                "required": ["origin", "destination"],
                "additionalProperties": False
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
            "description": "Measure the straight-line distance between two Gaode coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "minLength": 1, "description": "Start coordinate as `longitude,latitude`."},
                    "destination": {"type": "string", "minLength": 1, "description": "End coordinate as `longitude,latitude`."}
                },
                "required": ["origin", "destination"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "poi_keyword_search",
            "description": "Search POIs by keyword. Pass `city` when the search has a clear city scope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "minLength": 1, "description": "POI search keywords."},
                    "city": {"type": "string", "minLength": 1, "description": "Optional city scope."}
                },
                "required": ["keywords"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "poi_nearby_search",
            "description": "Search POIs around a center coordinate. `location` is `longitude,latitude`; `radius` is meters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "minLength": 1, "description": "POI search keywords."},
                    "location": {"type": "string", "minLength": 1, "description": "Center coordinate as `longitude,latitude`."},
                    "radius": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50000,
                        "description": "Search radius in meters. Default: 1000.",
                        "default": 1000,
                    }
                },
                "required": ["keywords", "location"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "poi_details",
            "description": "Get POI details by an ID returned from a POI search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1, "description": "POI ID returned by a POI search."}
                },
                "required": ["id"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "text_editor",
            "description": (
                "View or edit UTF-8 text files in the workspace. "
                "Commands: `view`, `str_replace`, `create`, `insert`. "
                "View immediately before editing; `str_replace` requires exactly one match."
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
                        "minLength": 1,
                        "description": "Workspace path, or `.` for the workspace root."
                    },
                    "view_range": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                        "description": "For `view`: `[start_line, end_line]`; lines start at 1, and `-1` means end."
                    },
                    "old_str": {
                        "type": "string",
                        "description": "For `str_replace`: exact existing text; it must occur exactly once."
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement text for `str_replace`, or alternate insert text for `insert`."
                    },
                    "file_text": {
                        "type": "string",
                        "description": "For `create`: complete initial file content; may be empty."
                    },
                    "insert_line": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "For `insert`: insert after this line; `0` inserts at the beginning."
                    },
                    "insert_text": {
                        "type": "string",
                        "description": "For `insert`: text to add after `insert_line`; use `new_str` instead if preferred."
                    }
                },
                "required": ["command", "path"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run non-interactive bash commands in the per-session workspace. "
                "CWD starts at `$HOME` / `$WORKSPACE`; do not leave the workspace or use `/tmp`. "
                "Use `download/` for user uploads, `upload/` for files to present, and `.runtime/` only for temporary files. "
                "Avoid interactive programs and daemons; use `restart=true` for a stuck session."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 120,
                        "description": "Required progress label: one short sentence explaining the command's purpose."
                    },
                    "command": {
                        "type": "string",
                        "description": "Bash command. Required unless restarting or using `task_action`."
                    },
                    "restart": {
                        "type": "boolean",
                        "description": "Reset the bash session before doing anything else.",
                        "default": False,
                    },
                    "timeout": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 600,
                        "description": "Foreground timeout in seconds. Default: 300; use for expected long quiet commands.",
                        "default": 300
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Run as a background task and return its task id immediately.",
                        "default": False
                    },
                    "task_action": {
                        "type": "string",
                        "enum": ["status", "output", "stop", "list"],
                        "description": "`status`/`output`/`stop` need `task_id`; `list` does not. Do not combine with `command` or `run_in_background`."
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Background task id for `status`, `output`, or `stop`."
                    }
                },
                # command 是功能上的必填字段（没有命令的 bash 调用无意义）：
                # 显式声明后，L2 schema 校验层能把「缺 command」以可操作
                # 错误回传模型自纠，strict 规范化也会正确将其保持为
                # 非可空必填，而不是被当作可选字段。
                # description 同样显式声明为必填：草稿消息（rich draft 的
                # 工具组/单工具块进行态摘要）依赖它展示命令意图；漏填时
                # L2 会拒绝并回传「补 description」的可操作错误，模型一
                # 轮自纠即可；strict 模式下保持非可空 string，不会被模型
                # 用 null 糊弄过去（null 会在 strip_null_arguments 后变成
                # 缺键，同样被 L2 拦截）。
                # 注意：command 不再列入 required（v2.5）——task_action 调用
                # （list/status 等）没有 command；两者互斥由执行器给可操作
                # 错误兜底。
                "required": ["description"],
                "additionalProperties": False
            },
            "input_examples": [
                {"description": "查看项目文件列表", "command": "ls -la"},
                {"description": "安装依赖并运行测试", "command": "pip install --user pytest && python3 -m pytest -q"},
                {"description": "读取用户上传的文档", "command": "head -c 2000 download/brief.pdf | strings | head -40"},
                {"description": "把报告放入发送暂存区", "command": "cp report.pdf upload/report.pdf"},
                {"description": "大型构建（长时间静默运行）", "command": "make -j4 && ctest --output-on-failure", "timeout": 600},
                {"description": "重启卡死的会话", "restart": True}
            ]
        }
    },
    {
        "type": "function",
        "function": {
            "name": "present_files",
            "description": (
                "Send one or more files from the upload/ staging directory to the chat "
                "as attachments. Pass workspace-relative paths under upload/ (e.g. "
                "`upload/out.txt`). Files outside upload/ are rejected — stage them "
                "first with bash (`cp out.txt upload/out.txt`). Absolute paths are "
                "accepted only when they resolve inside upload/. Wildcards are not supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "description": "Workspace-relative file paths under `upload/`."
                    }
                },
                "required": ["paths"],
                "additionalProperties": False
            }
        }
    },
    *(
        [{
            "type": "function",
            "function": {
                # 统一图像工具（原 generate_image_from_text / edit_image_with_reference
                # 合并）：操作语义由 image_url 是否提供决定——省略 = 文生图，
                # 提供 = 以该图为底编辑（图生图）。旧工具名仍可分发（别名
                # 兼容，见 tool_dispatch），但不再进入工具清单。
                # 显示口径（2026-09）：生成/编辑是"每次调用"的操作（由
                # image_url 是否携带决定），不是模型属性；模型只按"允许
                # 不允许携带 image_url"分两档——双能力模型（image_url 可选，
                # 同一端点不带 URL 即生成、带 URL 即编辑，如
                # agnes-image-2.5-flash）与仅生成模型（不接受 image_url）。
                # 不再把模型呈现成"非纯生成即纯编辑"的两个互斥桶。
                "name": "generate_image",
                "description": (
                    "Unified image tool: generate OR edit — the operation is decided PER CALL by whether "
                    "`image_url` is provided, NOT by the model. "
                    "CREATE — omit `image_url`: generates a brand-new image from the text prompt. "
                    "EDIT — provide `image_url` (an image URL from earlier tool results, a user-upload URL, or a base64 data URL): "
                    "modifies that exact image according to the prompt (style change, object add/remove, background, angle...) "
                    "while keeping the rest of the scene unchanged. "
                    "Models differ only in whether image_url is ALLOWED (dual-mode models run both operations on the same endpoint): "
                    f"Dual-mode models (image_url OPTIONAL — omit it to create, provide it to edit): {', '.join(DUAL_MODE_MODELS) if DUAL_MODE_MODELS else '(none)'}. "
                    f"Generate-only models (text-to-image; image_url NOT accepted): {', '.join(GENERATE_ONLY_MODELS) if GENERATE_ONLY_MODELS else '(none)'}. "
                    "Any model can CREATE (omit image_url); EDIT requires a dual-mode model. "
                    "Most calls only need prompt/model(/image_url); `extra_params` is an escape "
                    "hatch for provider-specific options this schema doesn't expose as a dedicated "
                    "field — see its own description before using it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Detailed image prompt or edit instruction."
                        },
                        "model": {
                            "type": "string",
                            "enum": TEXT_ONLY_MODELS,
                            "description": "Image model. Editing requires a dual-mode model."
                        },
                        "image_url": {
                            "type": "string",
                            "description": "Reference image URL or base64 data. Omit for generation; provide for editing."
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "enum": ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"],
                            "description": "Output aspect ratio. Default: `1:1`.",
                            "default": "1:1"
                        },
                        "image_size": {
                            "type": "string",
                            "enum": ["1K", "2K", "4K"],
                            "description": "Output resolution tier. Default: `1K`.",
                            "default": "1K"
                        },
                        "num_images": {
                            "type": "integer",
                            "default": 1,
                            "minimum": 1,
                            "maximum": 4,
                            "description": "Number of generated images. Create mode only; edit mode returns one image."
                        },
                        "extra_params": {
                            "type": "object",
                            "description": ("Optional provider-specific parameters not exposed above. "
                                "Do not duplicate `prompt`, `model`, `image_url`, `aspect_ratio`, or `image_size."),
                            "additionalProperties": True
                        }
                    },
                    "required": ["prompt", "model"],
                    "additionalProperties": False
                },
                "input_examples": [
                    # 纯文生图：任意模型（此处刻意用双能力模型演示——同一
                    # 模型省略 image_url 即生成，与下一例同模型不同操作）
                    {"prompt": "一只在月球上骑自行车的橘猫，赛博朋克风格", "model": TEXT_ONLY_MODELS[0] if TEXT_ONLY_MODELS else ""},
                    # 编辑：同一模型携带 image_url 即切换为编辑
                    {
                        "prompt": "移除场景中所有行人，保持其他内容完全不变",
                        "model": DUAL_MODE_MODELS[0] if DUAL_MODE_MODELS else "",
                        "image_url": "https://example.com/previous-image.png"
                    },
                    # extra_params 透传示例：显式要求 URL 输出而非工具默认选择
                    {
                        "prompt": "一座漂浮在云雾峡谷上空的发光城市，电影感写实风格",
                        "model": DUAL_MODE_MODELS[0] if DUAL_MODE_MODELS else "",
                        "extra_params": {"extra_body": {"response_format": "url"}}
                    }
                ]
            }
        }]
        if TEXT_ONLY_MODELS else []
    ),
    *(
        [{
            "type": "function",
            "function": {
                "name": "generate_video",
                "description": (
                    "Generate a short video from a text prompt. Do not use for GIFs or animated images. "
                    f"Available models: {', '.join(VIDEO_MODELS) if VIDEO_MODELS else '(none configured)'}"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "Video prompt: subject, motion, camera, and style."
                        },
                        "model": {
                            "type": "string",
                            "enum": VIDEO_MODELS,
                            "description": "Video generation model."
                        },
                        "duration": {
                            "type": "integer",
                            "description": "Duration in seconds. Default: 5.",
                            "default": 5,
                            "minimum": 3,
                            "maximum": 30
                        }
                    },
                    "required": ["prompt", "model"],
                    "additionalProperties": False
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


