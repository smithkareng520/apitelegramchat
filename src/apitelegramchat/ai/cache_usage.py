"""Prompt cache 命中观测：把各家的缓存命中字段统一提取成一份小字典，
在每轮请求结束后打一行 INFO 日志，让"缓存命中率"变成可度量的指标。

从 agentic_loops.py 拆出为独立模块：OpenAI 兼容循环与 Gemini 原生桥接
（gemini_bridge）共用本模块，且避免 gemini_bridge -> agentic_loops 的
循环导入。

字段来源：
  OpenAI / OpenRouter  : usage.prompt_tokens_details.cached_tokens
  Anthropic 风格        : cache_read_input_tokens
  DeepSeek 风格         : prompt_cache_hit_tokens
  Gemini 原生           : （gemini_bridge 归一为 OpenAI 形状 dict 后）
                          prompt_tokens_details.cached_tokens
                          （= usageMetadata.cachedContentTokenCount，隐式缓存）
口径说明（重要）：
  OpenAI 兼容接口中 prompt_tokens 为纯输入 token 数（输出单独记录在
  completion_tokens 中），cached_tokens 是 prompt_tokens 的子集，
  因此命中率 = Cached / Input_tokens，分母不含输出 token。
上报缺口（2026-09 实测）：agnes 网关流式 usage 的 prompt_tokens_details
  时有时无（同一逐字节相同请求连发数次，带与不带随机出现；带 tools 时
  甚至整块缺失）。缺失时无法区分"真实脱靶"与"命中但未上报"，该轮
  降为 DEBUG 不计入命中率统计，并用本轮最近一份带缓存字段的 usage
  快照（cache_hint）尽力补齐，避免把真实命中记成假 0。
"""
from apitelegramchat.utils import get_logger

logger = get_logger(__name__)


def _cached_from_usage(usage):
    """从 usage（dict 或 pydantic 对象）中只提取缓存命中数字。

    兼容三种字段形态：OpenAI prompt_tokens_details.cached_tokens /
    Anthropic 风格 cache_read_input_tokens / DeepSeek 风格
    prompt_cache_hit_tokens。取不到返回 None（=网关未上报）。
    """
    def _num(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        return None

    if usage is None:
        return None
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        for value in (
            details.get("cached_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("prompt_cache_hit_tokens"),
        ):
            num = _num(value)
            if num is not None:
                return num
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    num = _num(getattr(details, "cached_tokens", None)) if details is not None else None
    extra = getattr(usage, "model_extra", None)
    extra = extra if isinstance(extra, dict) else {}
    if num is None:
        num = _num(extra.get("cache_read_input_tokens"))
    if num is None:
        num = _num(extra.get("prompt_cache_hit_tokens"))
    return num


def _extract_cache_usage(usage, cache_hint=None) -> dict:
    if usage is None:
        return {}

    def _num(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        return None

    if isinstance(usage, dict):
        prompt = _num(usage.get("prompt_tokens"))
        completion = _num(usage.get("completion_tokens"))
        details = usage.get("prompt_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}
        cached = _num(details.get("cached_tokens"))
        if cached is None:
            cached = _num(usage.get("cache_read_input_tokens"))
        if cached is None:
            cached = _num(usage.get("prompt_cache_hit_tokens"))
    else:
        prompt = _num(getattr(usage, "prompt_tokens", None))
        completion = _num(getattr(usage, "completion_tokens", None))
        details = getattr(usage, "prompt_tokens_details", None)
        cached = _num(getattr(details, "cached_tokens", None)) if details is not None else None
        extra = getattr(usage, "model_extra", None)
        extra = extra if isinstance(extra, dict) else {}
        if cached is None:
            cached = _num(extra.get("cache_read_input_tokens"))
        if cached is None:
            cached = _num(extra.get("prompt_cache_hit_tokens"))

    # 缓存字段是否被网关真实上报（cached=0 也算上报；字段整体缺失=未上报）。
    # 未上报时用本轮最近一份带缓存字段的快照（cache_hint）尽力补齐。
    reported = cached is not None
    if cached is None and cache_hint is not None:
        hinted = _cached_from_usage(cache_hint)
        if hinted is not None:
            cached = hinted
            reported = True

    stats: dict = {}
    if prompt:
        # 命中率口径：Cached / Input_tokens（prompt_tokens 即纯输入，
        # cached_tokens 是其子集；分母不含输出 token）。
        stats["Input_tokens"] = prompt
        stats["Output_tokens"] = completion if completion is not None else 0
        stats["Cached"] = cached if cached is not None else 0
        stats["Hit_ratio"] = stats["Cached"] / max(1, prompt)
        # 仅供 _log_cache_usage 判断，展示前会被 pop 掉。
        stats["_reported"] = reported
    else:
        # 极端情况：usage 中没有 prompt_tokens 但有缓存字段，仍如实显示。
        if cached is not None:
            stats["Cached"] = cached
    return stats


def _log_cache_usage(api_label: str, usage, cache_hint=None) -> None:
    """每轮请求结束时打一行缓存命中摘要（无缓存字段时静默跳过）。"""
    try:
        stats = _extract_cache_usage(usage, cache_hint)
        if stats:
            reported = stats.pop("_reported", True)
            if "Hit_ratio" in stats:
                if not reported:
                    # 网关整轮未上报任何缓存字段（agnes 流式实测高发）：
                    # 记 0 会把"命中但未上报"伪装成"全脱靶"，污染命中率
                    # 统计，故降为 DEBUG，命中率均值只按真实上报的轮次算。
                    logger.debug(
                        "[%s] cache usage 未上报缓存字段（流式 usage 缺 "
                        "prompt_tokens_details，无法区分命中与脱靶）",
                        api_label,
                    )
                    return
                # 按固定键序格式化，Hit_ratio 以百分数显示（一位小数），
                # 避免直接 dump dict 导致字符串值带引号、浮点数带多余位数。
                logger.info(
                    "[%s] prompt cache usage: "
                    "{'Input_tokens': %s, 'Output_tokens': %s, "
                    "'Cached': %s, 'Hit_ratio': %.1f%%}",
                    api_label,
                    stats.get("Input_tokens", "-"),
                    stats.get("Output_tokens", "-"),
                    stats.get("Cached", "-"),
                    stats["Hit_ratio"] * 100,
                )
            else:
                logger.info("[%s] prompt cache usage: %s", api_label, stats)
    except Exception:
        # 观测日志绝不影响主流程
        logger.debug("cache usage 日志记录失败", exc_info=True)
