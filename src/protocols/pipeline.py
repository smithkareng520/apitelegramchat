# -*- coding: utf-8 -*-
"""统一请求管道：参数分层 -> 输入组合鉴权 -> API 分支 -> 请求体构建。

这是"一个用户回合到底怎么发出去"的四步流水线（全部配置驱动，无任何
按模型/厂商硬编码的分支）：

    ① resolve_effective_params(model_info)      参数分层：厂商默认 -> 模型覆盖
    ② resolve_input_combination(user_message)   输入组合：本轮用户发了什么模态
       authorize_request(params, combination)   鉴权：输入组合 vs 模型能力
    ③ resolve_request_plan(model_info)          分支：chat / images / video
                                                （api 类型 + 协议 + 端点 + 形状）
    ④ build_media_request_body(plan, ...)       请求体：按内容/api类型/端点装配

回合入口（ai_handlers.get_ai_response）只需要调一次 :func:`run_preflight`
就能拿到 ①②③ 的全部结论与日志摘要；④ 供媒体链路与工具层按计划装配
请求体。聊天（chat）分支的流式请求体由各协议循环装配（openai_chat /
anthropic_messages / gemini_native），采样与推理参数同样来自 ① 的
统一出口（get_sampling_params / get_reasoning_request_fields）——
本模块不重复实现协议循环，只负责"鉴权 + 分支 + 媒体请求体"。

鉴权语义（与 ai.attachment_content 的降级行为严格一致）：
  - 模态不支持 -> 降级（degrade）：附件转为文本占位，回合继续；
    音频另有转录降级路径。绝不静默丢弃。
  - 媒体分支（images/video）缺文本 prompt -> 硬性前置不满足（blocked）：
    上游必然 400，提前拦截并给用户可操作的提示（"请描述想要生成的内容"），
    不再让空 prompt 请求打到网关。
  - chat 分支永因缺文本 blocked（TIMER 合成唤醒、纯附件回合均合法）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional

from protocols.routing import ModelRoute, resolve_model_route

if TYPE_CHECKING:
    from config import ModelConfig

# API 分支标签（请求体家族）：
#   chat    OpenAI/Anthropic/Gemini 聊天协议（流式 agentic 循环装配请求体）
#   images  OpenAI Images 协议（生成/编辑/多图合成，media_generation 装配）
#   video   视频任务提交协议（media_generation._request_agnes_video 等装配）
ApiBranch = Literal["chat", "images", "video"]

# 输入模态标签（与 Telegram 附件 kind / EffectiveParams.capability_for_modality 对齐）
Modality = Literal["photo", "audio", "video", "document"]

_MEDIA_ROUTES: frozenset = frozenset({"image", "video"})
_ROUTE_TO_API: dict = {"image": "images", "video": "video", "chat": "chat"}


# ---------------------------------------------------------------------------
# ② 输入组合：本轮用户输入了什么
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InputCombination:
    """用户输入组合（文本 + 各模态附件数量的不可变快照）。"""
    text: str = ""
    photo_count: int = 0
    audio_count: int = 0
    video_count: int = 0
    document_count: int = 0

    @property
    def has_attachments(self) -> bool:
        return bool(self.photo_count or self.audio_count or self.video_count or self.document_count)

    @property
    def modalities(self) -> frozenset:
        """出现过的附件模态集合（计数 > 0 才算出现）。"""
        mods: set[str] = set()
        if self.photo_count:
            mods.add("photo")
        if self.audio_count:
            mods.add("audio")
        if self.video_count:
            mods.add("video")
        if self.document_count:
            mods.add("document")
        return frozenset(mods)

    def summary(self) -> str:
        has_text = bool(self.text.strip())
        if not has_text and not self.has_attachments:
            return "empty"
        parts = [f"text={len(self.text.strip())}字符"]
        if self.photo_count:
            parts.append(f"photo×{self.photo_count}")
        if self.audio_count:
            parts.append(f"audio×{self.audio_count}")
        if self.video_count:
            parts.append(f"video×{self.video_count}")
        if self.document_count:
            parts.append(f"document×{self.document_count}")
        return "+".join(parts)


def _count_kind(modality: Modality, count: dict, file_id: Any) -> None:
    if file_id:
        count[modality] = count.get(modality, 0) + 1


def resolve_input_combination(user_message: Optional[dict]) -> InputCombination:
    """从 Telegram 侧消息信封解析输入组合（纯函数，无 IO）。

    兼容两类信封（与 ai.attachment_content._resolve_multimodal_content
    同口径）：
      - 单一类型：type ∈ {photo, photo_group, video, video_group, document,
        document_group, audio, voice} + file_id / file_ids 数组；
      - 混合类型：attachments = [{kind, file_id}, ...]（打断合并产物）。
    text 取 msg.content（字符串）；缺失信封返回空组合（TIMER 唤醒等）。
    """
    if not isinstance(user_message, dict):
        return InputCombination()
    text = user_message.get("content", "")
    if not isinstance(text, str):
        text = ""
    counts: dict = {}

    msg_type = str(user_message.get("type") or "").strip().lower()
    kind_map = {
        "photo": "photo", "photo_group": "photo",
        "audio": "audio", "voice": "audio",
        "video": "video", "video_group": "video",
        "document": "document", "document_group": "document",
    }
    if msg_type in kind_map:
        modality = kind_map[msg_type]
        file_ids = user_message.get("file_ids")
        if isinstance(file_ids, list) and file_ids:
            for fid in file_ids:
                _count_kind(modality, counts, fid)
        else:
            _count_kind(modality, counts, user_message.get("file_id"))

    atts = user_message.get("attachments")
    if isinstance(atts, list):
        att_kind_map = {"photo": "photo", "image": "photo", "audio": "audio",
                        "voice": "audio", "video": "video", "document": "document"}
        for att in atts:
            if not isinstance(att, dict):
                continue
            kind = att_kind_map.get(str(att.get("kind") or "").strip().lower())
            if kind:
                _count_kind(kind, counts, att.get("file_id"))

    return InputCombination(
        text=text,
        photo_count=int(counts.get("photo", 0)),
        audio_count=int(counts.get("audio", 0)),
        video_count=int(counts.get("video", 0)),
        document_count=int(counts.get("document", 0)),
    )


# ---------------------------------------------------------------------------
# ② 鉴权：输入组合 vs 模型有效参数
# ---------------------------------------------------------------------------
@dataclass
class AuthVerdict:
    """鉴权结论：请求能否继续、哪些模态降级、媒体分支是否被硬性拦截。"""
    route: ModelRoute
    ok: bool = True
    # modality -> 降级原因（该模态附件将转为文本占位，回合继续）
    degraded: dict = field(default_factory=dict)
    # 媒体分支硬性前置不满足（如缺 prompt）：请求不应发出
    blocked: bool = False
    block_reason: str = ""

    def summary(self) -> str:
        parts = [f"route={self.route}", "ok" if self.ok else "fail"]
        if self.degraded:
            parts.append("degrade:" + ",".join(sorted(self.degraded)))
        if self.blocked:
            parts.append(f"blocked({self.block_reason})")
        return " ".join(parts)


def authorize_request(
    model_info: Optional["ModelConfig"],
    combination: InputCombination,
    params: Optional[Any] = None,
) -> AuthVerdict:
    """按"输入组合 × 模型有效参数"鉴权（统一出口，纯函数）。

    规则（与 attachment_content 的逐附件降级行为一致）：
      1. 每种出现过的附件模态查 params.capability_for_modality：
         不支持 -> 记入 degraded（请求层会转文本占位：图片/视频/文档占位，
         音频走转录），不阻断回合；
      2. 媒体分支（image/video 生成）要求本轮 user 消息有非空文本 prompt
         （与媒体循环"prompt 取最后一条 user 消息文本"同口径）：缺失 ->
         blocked（空 prompt 打到生成端点只会换来上游 400）；
      3. chat 分支永不因缺文本/纯附件 blocked。
    """
    from config import resolve_effective_params

    route = resolve_model_route(model_info)
    if params is None:
        params = resolve_effective_params(model_info)

    degraded: dict = {}
    for modality in sorted(combination.modalities):
        if not params.capability_for_modality(modality):
            degraded[modality] = f"模型不支持{modality}输入，将降级为文本占位"

    blocked = False
    block_reason = ""
    if route in _MEDIA_ROUTES and not combination.text.strip():
        blocked = True
        block_reason = (
            "请描述你想要生成的内容（例如：把背景换成夕阳海滩 / 生成一段5秒的城市夜景视频）。"
        )

    return AuthVerdict(
        route=route,
        ok=not blocked,
        degraded=degraded,
        blocked=blocked,
        block_reason=block_reason,
    )


# ---------------------------------------------------------------------------
# ③ API 分支：chat / images / video（协议 + 端点 + 形状）
# ---------------------------------------------------------------------------
@dataclass
class RequestPlan:
    """某模型本次请求的完整分支计划（数据驱动路由的解析结果）。"""
    route: ModelRoute
    api_type: ApiBranch
    protocol: str                  # 协议标签（openai_chat / openai_images / ...）
    endpoint: Optional[str]        # 声明的完整端点 URL；None = 按协议从 base_url 推导
    base_url: str
    image_style: Optional[str] = None   # 仅 images 分支：inline_images / multipart_edits
    session_affinity: bool = False
    vision_prefer_url: bool = False

    def describe(self) -> str:
        bits = [f"route={self.route}", f"api={self.api_type}", f"protocol={self.protocol}"]
        bits.append(f"endpoint={self.endpoint or '(协议推导)'}")
        if self.image_style:
            bits.append(f"shape={self.image_style}")
        return " ".join(bits)


def resolve_request_plan(model_info: Optional["ModelConfig"]) -> RequestPlan:
    """解析模型应进入的 API 分支（chat / images / video）+ 端点 + 形状。

    分支判断完全来自配置：能力字段决定链路（resolve_model_route），端点
    字段决定连到哪（get_effective_endpoint），图像形状由端点/inline 声明
    决定（media_generation.resolve_images_endpoint_shape）。新增一个模型
    不需要改这里任何代码。
    """
    from config import get_effective_endpoint

    route = resolve_model_route(model_info)
    api_type: ApiBranch = _ROUTE_TO_API[route]
    try:
        ep = get_effective_endpoint(model_info) if model_info is not None else None
    except ValueError:
        ep = None
    protocol = str(getattr(ep, "protocol", "") or "") if ep else ""
    base_url = str(getattr(ep, "base_url", "") or "") if ep else ""

    image_style: Optional[str] = None
    if api_type == "images":
        # 端点/形状解析复用 media_generation 的公共出口（惰性导入，
        # 避免 protocols -> ai 的模块级依赖）。
        try:
            from ai.media_generation import resolve_images_endpoint_shape
            shape = resolve_images_endpoint_shape(model_info)
            image_style = shape.style
        except Exception:
            image_style = None

    return RequestPlan(
        route=route,
        api_type=api_type,
        protocol=protocol,
        endpoint=(getattr(ep, "endpoint", None) if ep else None),
        base_url=base_url,
        image_style=image_style,
        session_affinity=bool(getattr(ep, "session_affinity", False)) if ep else False,
        vision_prefer_url=bool(getattr(ep, "vision_prefer_url", False)) if ep else False,
    )


# ---------------------------------------------------------------------------
# ④ 请求体构建：按（请求内容，api 类型，端点）装配
# ---------------------------------------------------------------------------
def build_media_request_body(
    plan: RequestPlan,
    *,
    model: str,
    prompt: str,
    reference_images: tuple = (),
    size: Optional[str] = None,
    ratio: Optional[str] = None,
    return_base64: bool = True,
) -> Optional[dict]:
    """按分支计划装配媒体请求体（当前覆盖 images 分支的 inline 形状）。

    - plan.api_type == "images" 且形状为 inline_images（Agnes 式）：
      返回完整 JSON 请求体（文生图 / 图生图 / 多图合成共用，硬约束见
      media_generation.build_inline_images_payload：response_format 只进
      extra_body、参考图数组 extra_body.image、不传 tags）；
    - multipart_edits 形状：编辑体是 multipart/form-data（文件字段），
      不适用 JSON 构建，返回 None（由 media_generation 按任务装配）；
    - chat / video 分支：请求体分别由协议循环 / 视频任务函数装配，
      返回 None。
    """
    if plan.api_type != "images" or plan.image_style != "inline_images":
        return None
    from ai.media_generation import build_inline_images_payload

    return build_inline_images_payload(
        model=model,
        prompt=prompt,
        size=size,
        ratio=ratio,
        image_data_urls=tuple(reference_images),
    )


# ---------------------------------------------------------------------------
# ①+②+③ 组合：回合入口的一次性预检
# ---------------------------------------------------------------------------
@dataclass
class TurnPreflight:
    """回合预检结果：有效参数 + 输入组合 + 鉴权结论 + 分支计划。"""
    params: Any                    # EffectiveParams（惰性类型避免循环导入）
    combination: InputCombination
    verdict: AuthVerdict
    plan: RequestPlan

    def describe(self) -> str:
        return (
            f"model={self.params.model_id or '(未注册)'} "
            f"input=[{self.combination.summary()}] "
            f"auth=[{self.verdict.summary()}] "
            f"plan=[{self.plan.describe()}]"
        )


def run_preflight(model_info: Optional["ModelConfig"], user_message: Optional[dict]) -> TurnPreflight:
    """回合入口的统一预检：参数分层 -> 输入组合 -> 鉴权 -> 分支。

    ai_handlers.get_ai_response 在模型解析后调用一次即可；返回值里
    plan.route 直接驱动回合分发（取代散落的 resolve_model_route 调用），
    verdict.blocked 直接短路媒体回合（不再发必败请求）。
    """
    from config import resolve_effective_params

    params = resolve_effective_params(model_info)
    combination = resolve_input_combination(user_message)
    verdict = authorize_request(model_info, combination, params=params)
    plan = resolve_request_plan(model_info)
    return TurnPreflight(
        params=params, combination=combination, verdict=verdict, plan=plan,
    )


__all__ = [
    "ApiBranch",
    "Modality",
    "InputCombination",
    "AuthVerdict",
    "RequestPlan",
    "TurnPreflight",
    "resolve_input_combination",
    "authorize_request",
    "resolve_request_plan",
    "build_media_request_body",
    "run_preflight",
]
