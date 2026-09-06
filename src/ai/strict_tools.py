"""OpenAI Structured Outputs（strict: true）注入与递归 schema 规范化。

这是工具参数 JSON 处理管线的第 0 层（预防层），主流依据：

- OpenAI Structured Outputs / function calling strict mode 是各家
  agent 框架（OpenAI Agents SDK、LangChain、LlamaIndex）从源头消灭
  "模型产出非法工具参数 JSON" 的标准手段：strict 模式下网关按 schema
  约束解码，参数 JSON 在语法层面几乎不可能再是坏的。
- strict 是 **per-tool** 的：同一请求里可以混用 strict 与非 strict 工具。
  因此本模块只对"能安全规范化"的工具注入 strict（bash / text_editor
  这类结构简单的核心工具优先受益），schema 含 union type / 根 anyOf
  等复杂构造的工具原样发送，绝不破坏其语义。

strict 模式对 schema 的硬性要求（OpenAI 官方文档），规范化时逐条满足：

1. 每一层 object：``additionalProperties: false``；
2. 每一层 object：``required`` 必须列出 ``properties`` 的**全部**键；
3. 原本可选的属性 → 可空类型 ``type: ["T", "null"]``（模型发 null
   表示"未提供"，执行层 ``schema_validation.strip_null_arguments``
   把 null 剥掉后分发，executor 的默认值语义完整保留）；
4. ``type`` 只能是字符串或 ``["T", "null"]`` 形式（union type 的工具
   直接判定为不适合 strict，不注入）；
5. 不支持的 ``default`` 关键字剥除（默认值语义已在 description 里）。

聚合网关对 strict 的支持参差不齐（OpenRouter / ModelScope / glm /
agnes 等转发厂商行为各异），因此配套**运行时自动降级**：

- 首次请求带 strict 的工具被网关 4xx 拒绝、且报错文本指向
  schema / strict / additionalProperties 时，标记该 api_label
  "strict 被拒"（进程内缓存），立即用**原始 schema** 重试一次；
- 后续所有轮次直接使用原始 schema，不再每轮撞墙；
- 全局手动开关：环境变量 ``DISABLE_STRICT_TOOL_SCHEMA=1``；
- Gemini OpenAI-compat 层官方文档未承诺 strict 支持，默认**不**注入
  （可用 ``ENABLE_STRICT_TOOL_SCHEMA_GEMINI=1`` 实验，同样带自动降级）。

安全约束：所有规范化都在深拷贝上进行，原始工具列表（SEARCH_TOOLS
等模块级常量）绝不被修改。
"""
import copy
import os
from typing import Any, Optional

from utils import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 开关与运行时降级状态
# ---------------------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "on"}
# api_label -> 被网关拒绝（进程内记忆，避免每轮重试撞墙）
_strict_rejected_labels: set = set()

# 报错文本命中这些特征才判定为"strict schema 被网关拒绝"（避免把
# 消息形状等无关 400 误判成 strict 问题而白白降级）。
_STRICT_REJECTION_TOKENS = (
    "strict",
    "additionalproperties",
    "structured output",
    "additional properties",
)


def _strict_env_disabled() -> bool:
    return os.getenv("DISABLE_STRICT_TOOL_SCHEMA", "").strip().lower() in _TRUTHY


def gemini_strict_env_enabled() -> bool:
    """Gemini OpenAI-compat 层默认不注入 strict；此环境变量用于实验。"""
    return os.getenv("ENABLE_STRICT_TOOL_SCHEMA_GEMINI", "").strip().lower() in _TRUTHY


def strict_rejected_labels() -> set:
    """当前进程内已判定『strict 被网关拒绝』的 api_label 集合（测试用）。"""
    return set(_strict_rejected_labels)


def reset_strict_rejection_state() -> None:
    """清空运行时降级状态（仅供测试）。"""
    _strict_rejected_labels.clear()


def mark_strict_tools_rejected(api_label: str, reason: str = "") -> None:
    """记录该 api_label 的网关拒绝了 strict 工具 schema，此后不再注入。"""
    if api_label in _strict_rejected_labels:
        return
    _strict_rejected_labels.add(api_label)
    logger.warning(
        "[%s] 网关拒绝了 strict 工具 schema%s：已自动降级为原始 schema，"
        "本轮及后续请求不再注入 strict（进程内记忆）",
        api_label,
        f"（{reason[:160]}）" if reason else "",
    )


def looks_like_strict_tool_rejection(error_text: str) -> bool:
    """判断一个 4xx 报错文本是否指向 strict / schema 规范化问题。"""
    try:
        text = (error_text or "").lower()
        if not text:
            return False
        if any(tok in text for tok in _STRICT_REJECTION_TOKENS):
            return True
        return "tool" in text and any(
            tok in text for tok in ("schema", "not support", "unsupported", "invalid")
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 递归规范化
# ---------------------------------------------------------------------------

def _unsupported_for_strict(schema: Optional[dict], depth: int = 0) -> bool:
    """递归判定 schema 是否含 strict 模式无法安全表达的构造。

    保守判定（宁可少注入一个工具，也不能改变 schema 语义）：
    - ``anyOf / oneOf / allOf / not``（含属性级与根级）；
    - 非 ``["T", "null"]`` 形式的 union type（如 ``["string", "array"]``）；
    - 无 ``type`` 也无 ``properties`` 的裸 schema（无法补全类型）；
    - 嵌套超过 8 层。
    """
    if not isinstance(schema, dict) or depth > 8:
        return True
    if any(k in schema for k in ("anyOf", "oneOf", "allOf", "not")):
        return True
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        if len(non_null) > 1 or not non_null:
            return True
    props = schema.get("properties")
    if t == "object" or isinstance(props, dict):
        if props is None:
            return False
        if not isinstance(props, dict):
            return True
        return any(_unsupported_for_strict(sub, depth + 1) for sub in props.values())
    if t == "array" or "items" in schema:
        return _unsupported_for_strict(schema.get("items", {}), depth + 1)
    if t is None:
        return True  # 无 type 的标量节点：无法在 strict 下表达
    return False


def _make_nullable(sub: dict) -> dict:
    """把（深拷贝后的）属性 schema 变为可空：type -> [T, "null"]。"""
    t = sub.get("type")
    if isinstance(t, list):
        if "null" not in t:
            sub["type"] = list(t) + ["null"]
        return sub
    if isinstance(t, str):
        sub["type"] = [t, "null"]
        if "enum" in sub and None not in (sub["enum"] or []):
            sub["enum"] = list(sub["enum"] or []) + [None]
    return sub


_OBJECT_KEEP_KEYS = ("description",)
_ARRAY_KEEP_KEYS = ("description", "minItems", "maxItems")
_SCALAR_KEEP_KEYS = (
    "description", "enum", "minimum", "maximum",
    "minLength", "maxLength", "pattern", "format",
)


def _normalize_for_strict(schema: dict, *, root: bool, depth: int = 0) -> dict:
    """递归规范化为 OpenAI strict 兼容形状（输入已是深拷贝，就地改写）。"""
    t = schema.get("type")
    props = schema.get("properties")

    if root or t == "object" or isinstance(props, dict):
        props = props if isinstance(props, dict) else {}
        required_orig = set(schema.get("required") or [])
        norm_props = {}
        for key, sub in props.items():
            norm = _normalize_for_strict(sub, root=False, depth=depth + 1)
            if key not in required_orig:
                # strict 要求全键必填；原本可选的键改为可空，模型发 null
                # 表示"未提供"，执行层剥掉 null 后走 executor 默认值。
                norm = _make_nullable(norm)
            norm_props[key] = norm
        out = {
            "type": "object",
            "properties": norm_props,
            "additionalProperties": False,
            "required": list(props.keys()),
        }
        for k in _OBJECT_KEEP_KEYS:
            if k in schema:
                out[k] = schema[k]
        return out

    if t == "array" or "items" in schema:
        out = {"type": t if isinstance(t, str) else "array"}
        if "items" in schema:
            out["items"] = _normalize_for_strict(
                schema["items"], root=False, depth=depth + 1)
        for k in _ARRAY_KEEP_KEYS:
            if k in schema:
                out[k] = schema[k]
        return out

    # 标量节点：保留受支持的关键字，剥除 default（strict 不支持）。
    out = {k: schema[k] for k in _SCALAR_KEEP_KEYS if k in schema}
    out["type"] = t
    return out


def _strictify_tool(tool: Any) -> Optional[dict]:
    """尝试把一个 OpenAI 形状的工具定义规范化为 strict 版本。

    返回带 ``strict: true`` 的**深拷贝**；工具不适合 strict（schema 含
    复杂构造 / 作者已显式声明 strict）时返回 ``None``，调用方原样发送。
    """
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function")
    if not isinstance(fn, dict):
        return None
    if fn.get("strict") is not None:
        # schema 作者已显式控制 strict 位：尊重作者，不改写。
        return None
    parameters = fn.get("parameters")
    if not isinstance(parameters, dict):
        return None
    if _unsupported_for_strict(parameters):
        return None
    strict_fn = {
        "name": fn.get("name", ""),
        "description": fn.get("description", "") or "",
        "parameters": _normalize_for_strict(copy.deepcopy(parameters), root=True),
        "strict": True,
    }
    return {"type": "function", "function": strict_fn}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def strict_tools_for_request(api_label: str, tools: Optional[list]) -> Optional[list]:
    """为请求准备工具列表：能规范化的工具注入 strict，其余原样。

    - 原始列表绝不被修改（ ineligible 工具按引用透传，规范化在深拷贝上
      进行）；
    - 没有任何工具获得 strict 时返回原列表本身（零开销）；
    - 开关 / 运行时降级 / api_label 粒度见模块头注释。
    """
    try:
        if not tools:
            return tools
        if _strict_env_disabled():
            return tools
        if api_label in _strict_rejected_labels:
            return tools
        out: list = []
        any_strict = False
        for tool in tools:
            strict_tool = _strictify_tool(tool)
            if strict_tool is None:
                out.append(tool)
            else:
                out.append(strict_tool)
                any_strict = True
        if not any_strict:
            return tools
        return out
    except Exception:
        # 预防层绝不阻塞主流程：规范化失败就当没这层。
        logger.warning("strict_tools_for_request 内部异常，回退原始工具列表",
                       exc_info=True)
        return tools
