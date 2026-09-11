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
    # 合并后的端点 URL（get_effective_endpoint().endpoint）：chat 等协议为
    # API 根（SDK 自拼标准路径）；图像模型声明了完整图像端点时即为请求 URL。
    endpoint: str = ""
    image_style: Optional[str] = None   # 仅 images 分支：inline_images / multipart_edits
    session_affinity: bool = False

    def describe(self) -> str:
        bits = [f"route={self.route}", f"api={self.api_type}", f"protocol={self.protocol}"]
        bits.append(f"endpoint={self.endpoint or '(未配置)'}")
        if self.image_style:
            bits.append(f"shape={self.image_style}")
        return " ".join(bits)


def resolve_request_plan(model_info: Optional["ModelConfig"]) -> RequestPlan:
    """解析模型应进入的 API 分支（chat / images / video）+ 端点 + 形状。

    分支判断完全来自配置：能力字段决定链路（resolve_model_route），端点
    字段决定连到哪（get_effective_endpoint 的唯一 endpoint），图像形状由
    endpoint 指向的 URL 路径推导
    （media_generation.resolve_images_endpoint_shape）。新增一个模型
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
    endpoint_url = str(getattr(ep, "endpoint", "") or "") if ep else ""

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
        endpoint=endpoint_url,
        image_style=image_style,
        session_affinity=bool(getattr(ep, "session_affinity", False)) if ep else False,
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
    - chat 分支：请求体由协议循环装配，返回 None；
    - video 分支：使用 :func:`build_video_request_body`（本文件下方）。
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


# Agnes Video 2.5 文档硬约束（https://wiki.agnes-ai.com）：
#   - 时长字段叫 seconds（字符串 "4"–"12"，默认 "5"）——发 duration 会被
#     网关 400 "duration is not an allowed request field"（2026-09-11 生产事故）；
#   - mode 必填：text（纯文本，禁止携带任何媒体字段）/ keyframe（首尾帧）/
#     reference（images/audios/videos 至少一类非空）；
#   - size 档位：720P / 1080P / 1K / 2K（不接受像素尺寸）；画幅用
#     aspect_ratio（21:9/16:9/4:3/1:1/3:4/9:16，不接受 auto）；
#   - 图片参考最多 8 张、视频参考最多 1 个、参考媒体总数 <= 12；
#   - 提示词建议显式写出素材占位符（<Picture N> / <Video N>，数组内从 1
#     开始编号）说明用途，可控性更好。
_VIDEO_SIZE_TIERS = frozenset({"720P", "1080P", "1K", "2K"})
_VIDEO_RATIOS = frozenset({"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"})
_VIDEO_MODES = frozenset({"text", "keyframe", "reference"})
_VIDEO_MAX_IMAGES = 8
_VIDEO_MAX_AUDIOS = 3
_VIDEO_MAX_REFERENCE_VIDEOS = 1
_VIDEO_SECONDS_MIN, _VIDEO_SECONDS_MAX, _VIDEO_SECONDS_DEFAULT = 4, 12, 5


def normalize_video_seconds(seconds: Any = None) -> str:
    """把任意输入（int/str/None）归一为文档合法的 seconds 字符串。

    - "4"–"12" 之外一律钳到边界；非数字回退默认 "5"；
    - 输出恒为字符串（文档要求 seconds 是 string，发 int 同样 400）。
    """
    try:
        value = int(str(seconds).strip())
    except (TypeError, ValueError):
        value = _VIDEO_SECONDS_DEFAULT
    value = max(_VIDEO_SECONDS_MIN, min(value, _VIDEO_SECONDS_MAX))
    return str(value)


def _normalize_video_size(size: Optional[str]) -> Optional[str]:
    value = str(size or "").strip().upper()
    return value if value in _VIDEO_SIZE_TIERS else None


def _normalize_video_ratio(ratio: Optional[str]) -> Optional[str]:
    value = str(ratio or "").strip()
    return value if value in _VIDEO_RATIOS else None


def _normalize_video_seed(seed: Any) -> Optional[int]:
    """归一化 seed：仅接受真实整数（bool 不是合法 seed），非法返回 None。"""
    if seed is None or isinstance(seed, bool):
        return None
    try:
        return int(seed)
    except (TypeError, ValueError):
        return None


def _normalize_video_specs(specs: Any) -> list[dict]:
    """归一化参考视频对象数组（文档：videos[].{url, start_seconds, require_audio}）。

    - 字符串元素视为纯 URL（{url} 最小形状）；
    - dict 元素：url 必填，start_seconds（正数才有意义，默认 0 不发送）、
      require_audio（布尔才发送，默认 false 不发送）均为可选；
    - 无 url 的条目直接丢弃，绝不发送空 url 让网关 400。
    """
    out: list[dict] = []
    for spec in specs or ():
        if isinstance(spec, str):
            url = spec.strip()
            if url:
                out.append({"url": url})
            continue
        if not isinstance(spec, dict):
            continue
        url = str(spec.get("url") or "").strip()
        if not url:
            continue
        obj: dict[str, Any] = {"url": url}
        start = spec.get("start_seconds")
        if isinstance(start, (int, float)) and not isinstance(start, bool) and start > 0:
            obj["start_seconds"] = int(start) if float(start).is_integer() else float(start)
        require_audio = spec.get("require_audio")
        if isinstance(require_audio, bool) and require_audio:
            # 文档默认 false：仅显式要求音轨时才发送
            obj["require_audio"] = True
        out.append(obj)
    return out


def _resolve_video_mode(
    mode: Optional[str],
    images: list,
    audios: list,
    videos: list,
    first_frame: Optional[str],
    last_frame: Optional[str],
) -> str:
    """解析生成模式（文档硬约束：mode 必填，且必须与媒体字段匹配）。

    - 显式 mode（text/keyframe/reference）优先，但按文档规则校验媒体：
      keyframe 需首尾帧至少其一、reference 需参考媒体至少一类非空，
      不满足时安全回退 text（绝不发出"模式与媒体不匹配"的必败请求）；
    - 未指定（None/空/非法值）自动推断：有首尾帧 -> keyframe；有参考
      媒体 -> reference；否则 text。与媒体循环的既有推断语义一致。
    """
    explicit = str(mode or "").strip().lower()
    if explicit in _VIDEO_MODES:
        if explicit == "text":
            return "text"
        if explicit == "keyframe":
            return "keyframe" if (first_frame or last_frame) else "text"
        if explicit == "reference":
            return "reference" if (images or audios or videos) else "text"
    if first_frame or last_frame:
        return "keyframe"
    if images or audios or videos:
        return "reference"
    return "text"


def build_video_request_body(
    plan: RequestPlan,
    *,
    model: str,
    prompt: str,
    seconds: Any = None,
    reference_images: tuple = (),
    reference_videos: tuple = (),
    size: Optional[str] = None,
    aspect_ratio: Optional[str] = None,
    mode: Optional[str] = None,
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    seed: Any = None,
    reference_audios: tuple = (),
    video_specs: tuple = (),
) -> Optional[dict]:
    """按分支计划装配视频任务请求体（Agnes Video 2.5 文档 schema）。

    规则（全部来自文档硬约束，无按厂商分支）：
      - plan.api_type != "video" -> None（其他分支各有装配出口）；
      - mode 必填：显式指定（text/keyframe/reference）经媒体匹配校验后
        生效，未指定时自动推断（首尾帧 -> keyframe；参考媒体 ->
        reference；纯文本 -> text）；
      - keyframe：first_frame/last_frame 顶层字符串（至少其一），禁止
        images/audios/videos；reference：images(<=8)/audios(<=3)/
        videos 对象数组(<=1, {url, start_seconds?, require_audio?})，
        禁止 first/last_frame；text：绝不携带任何媒体字段；
      - seconds 恒为字符串且钳在 "4"–"12"；size / aspect_ratio 仅在合法
        白名单内才发送；seed 仅接受整数；n 不发送（默认且仅支持 1）；
      - reference 模式在 prompt 末尾追加 <Picture N>/<Audio N>/<Video N>
        占位符说明（文档建议：显式说明素材用途可控性更好）。
    """
    if plan.api_type != "video":
        return None

    images = [str(u) for u in reference_images if str(u or "").strip()][:_VIDEO_MAX_IMAGES]
    audios = [str(u) for u in reference_audios if str(u or "").strip()][:_VIDEO_MAX_AUDIOS]
    specs = _normalize_video_specs(video_specs)
    spec_urls = {s["url"] for s in specs}
    plain_video_urls = [str(u) for u in reference_videos if str(u or "").strip()]
    videos = (specs + [{"url": u} for u in plain_video_urls if u not in spec_urls])
    videos = videos[:_VIDEO_MAX_REFERENCE_VIDEOS]
    first_frame = str(first_frame or "").strip() or None
    last_frame = str(last_frame or "").strip() or None

    resolved_mode = _resolve_video_mode(mode, images, audios, videos, first_frame, last_frame)

    clean_prompt = str(prompt or "").strip()
    if resolved_mode == "reference":
        tokens = [f"<Picture {i}>" for i in range(1, len(images) + 1)]
        tokens += [f"<Audio {i}>" for i in range(1, len(audios) + 1)]
        tokens += [f"<Video {i}>" for i in range(1, len(videos) + 1)]
        clean_prompt = (
            f"{clean_prompt}\n\n参考素材：{'、'.join(tokens)}。"
            "请结合参考素材的内容/风格/动作生成视频。"
        ).strip()

    payload: dict[str, Any] = {
        "model": model,
        "prompt": clean_prompt,
        "seconds": normalize_video_seconds(seconds),
        "mode": resolved_mode,
    }
    norm_size = _normalize_video_size(size)
    if norm_size:
        payload["size"] = norm_size
    norm_ratio = _normalize_video_ratio(aspect_ratio)
    if norm_ratio:
        payload["aspect_ratio"] = norm_ratio
    norm_seed = _normalize_video_seed(seed)
    if norm_seed is not None:
        payload["seed"] = norm_seed
    if resolved_mode == "keyframe":
        if first_frame:
            payload["first_frame"] = first_frame
        if last_frame:
            payload["last_frame"] = last_frame
    elif resolved_mode == "reference":
        if images:
            payload["images"] = images
        if audios:
            payload["audios"] = audios
        if videos:
            payload["videos"] = videos
    return payload


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
    "build_video_request_body",
    "normalize_video_seconds",
    "run_preflight",
]
