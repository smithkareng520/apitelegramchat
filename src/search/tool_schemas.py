"""媒体生成工具目录与 schema 数据底座（自 search_engine.py 拆出后重构）。

职责收敛
--------
1. 图像/视频模型能力目录（TEXT_ONLY_MODELS / EDIT_MODELS /
   DUAL_MODE_MODELS / GENERATE_ONLY_MODELS / VIDEO_MODELS，由
   SUPPORTED_MODELS 按能力推导）；
2. 统一图像工具 generate_image（文生图 + 图生图编辑）与视频工具
   generate_video 的工具定义 —— 两者是 host 内建工具（依赖 api_client 与
   Telegram 交付路径，不经 MCP）。

检索 / 地图 / todo / memory / bash / text_editor 等工具已全部 MCP 化：
schema 单一来源是 mcpserver/catalogue.py（内部 stdio 服务器）与外部
mcp.json 服务器（如 gaode_mcp）的 list_tools；模型视角名称由
tool_registry 统一装配为 ``mcp__<server>__<tool>``。
"""


from config import SUPPORTED_MODELS

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


# =============================================================================
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
# =============================================================================
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


def build_media_tool_defs() -> list[dict]:
    """host 内建媒体生成工具定义（按模型目录可用性裁剪）。"""
    defs: list[dict] = []
    if TEXT_ONLY_MODELS:
        defs.append(_generate_image_tool())
    if VIDEO_MODELS:
        defs.append(_generate_video_tool())
    return defs


def _generate_image_tool() -> dict:
    return {
        "type": "function",
        "function": {
            # 统一图像工具：操作语义由 image_url 是否提供决定——省略 = 文生图，
            # 提供 = 以该图为底编辑（图生图）。
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
    }


def _generate_video_tool() -> dict:
    return {
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
    }
