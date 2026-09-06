"""工具参数的 JSON Schema 语义校验层（分发前的最后一道闸）。

主流做法（LangChain StructuredTool / OpenAI Agents SDK / LlamaIndex
的收敛模式）：``json.loads`` 只能保证参数「语法是 JSON」，不能保证
「语义符合工具 schema」——缺必填字段、类型错误、枚举外取值这些
问题原本要到执行器里才暴露，模型只能拿到一句笼统的失败。主流
管线在**分发之前**用 JSON Schema 校验参数，校验失败把结构化、
可操作的错误作为工具结果回传，模型据此一轮自纠。

本模块提供：

1. ``strip_null_arguments`` —— strict 结构化输出（strict_tools.py）
   会把可选字段表达为 null（``type: ["T", "null"]``）；null 在分发
   前剥掉，executor 里 ``fn_args.get(k, default)`` 的默认值语义
   完整保留（键存在但为 None 时 ``.get`` 不会取默认值，必须先剥）。
2. ``coerce_common_slops`` —— 项目历史上明确容忍的两类"能无歧义
   恢复"的写法先做无损矫正再校验：布尔写成字符串（"true"/"false"）、
   数字写成字符串（"30"）。矫正后的参数直接进入分发，省一轮模型
   重试（与 deliver_reply 原有的字符串布尔容错语义一致）。
3. ``validate_tool_arguments`` —— 按工具真实 schema 校验：
   - 优先使用 ``jsonschema`` 社区标准库（requirements 已声明）；
   - 库缺失时退回内置轻量校验器（required / type / enum /
     items / anyOf 必填二选一），核心能力不依赖第三方包；
   - **对未知多余键宽容**（额外属性不算错误）：本项目约定模型可在
     参数里携带 ``_description`` / ``_summary`` 等展示性键，且
     各家 provider 对 additionalProperties 的支持不一，按主流
     实践只校验「声明的字段是否符合声明」，不惩罚额外字段。

错误消息以 ``Error:`` 开头（供 ``_tool_result_is_failure`` 失败判定
与工具连击熔断的签名归一化使用），并给出与 json_repair 诊断消息
同风格的可操作修复指引。
"""
import json
from typing import Any, Optional

from utils import get_logger

logger = get_logger(__name__)

_MAX_PROBLEMS = 6
_ARGUMENTS_EXCERPT_LIMIT = 400

# ---------------------------------------------------------------------------
# 参数预处理：null 剥离 + 常见写法矫正
# ---------------------------------------------------------------------------

def strip_null_arguments(fn_args: dict) -> dict:
    """剥掉值为 None 的键（strict 模式的 null = 未提供）。

    返回新 dict（不修改调用方传入的对象）；非 dict 输入原样返回。
    False / "" / 0 是有语义的取值，不属于"未提供"，不剥。
    """
    if not isinstance(fn_args, dict) or not fn_args:
        return fn_args
    return {k: v for k, v in fn_args.items() if v is not None}


def find_tool_schema(fn_name: str, tools: Optional[list]) -> Optional[dict]:
    """在工具列表中按名称查找 OpenAI 形状的 parameters schema。"""
    if not tools or not fn_name:
        return None
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name") == fn_name:
            params = fn.get("parameters")
            if isinstance(params, dict):
                return params
    return None


def _coerce_property(value: Any, prop_schema: Optional[dict]) -> Any:
    """对单个属性做无损矫正（仅在 schema 声明与实际类型不一致时生效）。"""
    if not isinstance(prop_schema, dict) or not isinstance(value, str):
        return value
    declared = prop_schema.get("type")
    if isinstance(declared, list):
        declared = declared[0] if declared else None
    text = value.strip()
    if declared == "boolean" and text.lower() in ("true", "false"):
        return text.lower() == "true"
    if declared in ("integer", "number"):
        try:
            num = float(text)
        except ValueError:
            return value
        if declared == "integer":
            if num.is_integer():
                return int(num)
            return value
        return num
    return value


def coerce_common_slops(fn_args: dict, schema: Optional[dict]) -> dict:
    """布尔 / 数字写成字符串时按 schema 矫正（无损、可判定才矫正）。"""
    if not isinstance(fn_args, dict) or not isinstance(schema, dict) or not fn_args:
        return fn_args
    props = schema.get("properties")
    if not isinstance(props, dict):
        return fn_args
    out = dict(fn_args)
    for key, value in out.items():
        prop_schema = props.get(key)
        if prop_schema is not None:
            out[key] = _coerce_property(value, prop_schema)
    return out


# ---------------------------------------------------------------------------
# 校验器：jsonschema 优先，内置兜底
# ---------------------------------------------------------------------------

def _try_import_jsonschema() -> Any:
    try:
        import jsonschema  # noqa: F401
        return jsonschema
    except Exception:
        return None


_jsonschema_mod = _try_import_jsonschema()


def _iter_jsonschema_errors(args: dict, schema: dict) -> list:
    """用 jsonschema 校验并格式化错误（宽容未知额外键）。"""
    import jsonschema
    problems: list = []
    try:
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        validator = cls(schema)
        for err in validator.iter_errors(args):
            if err.validator == "additionalProperties":
                # 未知额外键：本项目约定模型可带 _description 等展示键，
                # 主流实践也只校验「声明的字段是否符合声明」，不惩罚额外字段。
                continue
            if err.validator in ("anyOf", "oneOf") and err.context:
                # union / 二选一约束：钻进子错误提取字段级病因
                # （"''query' is a required property" 等），比一句笼统的
                # "not valid under any of the given schemas" 可操作得多。
                sub_msgs: list = []
                for sub in err.context:
                    msg = str(sub.message)
                    if msg not in sub_msgs:
                        sub_msgs.append(msg)
                    if len(sub_msgs) >= 3:
                        break
                problems.append((
                    list(err.absolute_path), " / ".join(sub_msgs) or str(err.message)))
            else:
                problems.append((list(err.absolute_path), err.message))
            if len(problems) >= _MAX_PROBLEMS:
                break
    except Exception:
        logger.warning("jsonschema 校验内部异常，退回内置校验器", exc_info=True)
        return []
    return problems


# ---------------------------------------------------------------------------
# 内置轻量校验器（jsonschema 不可用时的兜底；覆盖主要失败模式）
# ---------------------------------------------------------------------------

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _declared_type(prop_schema: dict) -> Optional[str]:
    t = prop_schema.get("type")
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        for candidate in t:
            if candidate != "null":
                return candidate
    return None


def _builtin_validate(args: dict, schema: dict) -> list:
    """required / type / enum / items / anyOf-必填二选一 的内置校验。"""
    problems: list = []
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    for key in (schema.get("required") or []):
        if key not in args:
            problems.append(([], f"'{key}' is a required property but is missing"))
            if len(problems) >= _MAX_PROBLEMS:
                return problems
    for key, value in args.items():
        prop = props.get(key)
        if prop is None or not isinstance(prop, dict):
            continue  # 未声明的额外键：宽容
        declared = _declared_type(prop)
        if declared and declared in _TYPE_CHECKS and not _TYPE_CHECKS[declared](value):
            problems.append((
                [key],
                f"'{key}' must be of type {declared}, "
                f"but a {type(value).__name__} was provided",
            ))
            if len(problems) >= _MAX_PROBLEMS:
                return problems
        enum = prop.get("enum")
        if isinstance(enum, list) and value not in enum:
            problems.append((
                [key],
                f"'{key}' must be one of {enum}, but {value!r} was provided"))
            if len(problems) >= _MAX_PROBLEMS:
                return problems
        items = prop.get("items")
        if isinstance(value, list) and isinstance(items, dict):
            item_type = _declared_type(items)
            if item_type and item_type in _TYPE_CHECKS:
                for i, item in enumerate(value):
                    if not _TYPE_CHECKS[item_type](item):
                        problems.append((
                            [key, str(i)],
                            f"item {i} of '{key}' must be of type {item_type}, "
                            f"but a {type(item).__name__} was provided",
                        ))
                        break
    if problems:
        return problems
    # 根级 anyOf 全部由 {required: [...]} 分支构成（web_search 的
    # query / image_url 二选一）：一个都没有时报可读错误。
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and any_of and all(
            isinstance(b, dict) and set(b) <= {"required"} and b.get("required")
            for b in any_of):
        if not any(all(k in args for k in b["required"]) for b in any_of):
            alternatives = " / ".join(
                ", ".join(str(k) for k in b["required"]) for b in any_of)
            problems.append(([], (
                f"at least one of these parameter groups must be provided: "
                f"{alternatives}")))
    return problems


# ---------------------------------------------------------------------------
# 错误消息渲染（与 json_repair 的诊断消息同风格）
# ---------------------------------------------------------------------------

def _summarize_params(schema: dict) -> tuple:
    """从 schema 提取 (必填列表, 可选列表)，每项形如 'command (string)'。"""
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    required = set(schema.get("required") or [])
    req_list: list[str] = []
    opt_list: list[str] = []
    for key, prop in props.items():
        if not isinstance(prop, dict):
            continue
        declared = _declared_type(prop)
        name = f"{key} ({declared})" if declared else str(key)
        (req_list if key in required else opt_list).append(name)
    return req_list, opt_list


def _render_problems(problems: list) -> list:
    lines: list = []
    for path, message in problems[:_MAX_PROBLEMS]:
        prefix = f"'{'.'.join(str(p) for p in path)}': " if path else ""
        lines.append(f"- {prefix}{message}")
    return lines


def _build_schema_error_message(
        fn_name: str, fn_args: dict, schema: dict, problems: list) -> str:
    first_line = (
        f"Error: tool {fn_name} was NOT executed: its arguments are valid JSON but "
        "failed validation against the tool's parameter schema."
    )
    parts = [first_line, "", "[Problems]"]
    parts.extend(_render_problems(problems))
    try:
        excerpt = json.dumps(fn_args, ensure_ascii=False)
    except Exception:
        excerpt = str(fn_args)
    if len(excerpt) > _ARGUMENTS_EXCERPT_LIMIT:
        excerpt = excerpt[:_ARGUMENTS_EXCERPT_LIMIT] + "…"
    parts.append("")
    parts.append(f"[Your arguments] {excerpt}")
    parts.append("")
    parts.append(
        f"[How to fix] Reissue the SAME tool call ({fn_name}) with corrected "
        "arguments that match the schema:")
    req_list, opt_list = _summarize_params(schema)
    if req_list:
        parts.append(f"- Required parameters: {', '.join(req_list)}")
    if opt_list:
        parts.append(f"- Optional parameters: {', '.join(opt_list)}")
    parts.append(
        "Do not change the tool or the task — only fix the arguments themselves "
        "(correct types, include every required parameter, use only declared "
        "parameter names)."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def validate_tool_arguments(
        fn_name: str, fn_args: dict, tools: Optional[list]) -> Optional[str]:
    """校验工具参数是否符合该工具的 schema。

    返回 ``None`` 表示通过（或无法校验——找不到 schema / 参数非 dict，
    这类情况交由上游信封层或执行器处理）；否则返回给模型的可操作
    错误消息（``Error:`` 开头）。

    注意：调用方应先做 ``strip_null_arguments``（null = 未提供）与
    ``coerce_common_slops``（字符串布尔/数字矫正），再进本校验。
    """
    try:
        if not isinstance(fn_args, dict) or not tools:
            return None
        schema = find_tool_schema(fn_name, tools)
        if not isinstance(schema, dict):
            return None
        if _jsonschema_mod is not None:
            problems = _iter_jsonschema_errors(fn_args, schema)
            if not problems:
                problems = _builtin_validate(fn_args, schema)
        else:
            problems = _builtin_validate(fn_args, schema)
        if not problems:
            return None
        message = _build_schema_error_message(fn_name, fn_args, schema, problems)
        logger.warning(
            "工具 %s 参数未通过 schema 校验（已拦截并回传可操作错误）：%s",
            fn_name, "; ".join(_render_problems(problems))[:300],
        )
        return message
    except Exception:
        # 校验层绝不阻塞主流程：异常时放行，交给执行器兜底。
        logger.warning("validate_tool_arguments 内部异常，放行参数", exc_info=True)
        return None


def normalize_and_validate(
        fn_name: str, fn_args: dict, tools: Optional[list],
) -> tuple:
    """主流分发前管线：null 剥离 → 常见写法矫正 → schema 校验。

    返回 ``(规范化后的参数, 错误消息或 None)``；错误非 None 时调用方
    不应执行工具，而是把消息作为该工具的结果回传给模型自纠。
    任何内部异常都放行原参数（不阻塞主流程）。
    """
    try:
        if not isinstance(fn_args, dict):
            return fn_args, None
        normalized = strip_null_arguments(fn_args)
        schema = find_tool_schema(fn_name, tools) if tools else None
        normalized = coerce_common_slops(normalized, schema)
        error = validate_tool_arguments(fn_name, normalized, tools)
        return normalized, error
    except Exception:
        logger.warning("normalize_and_validate 内部异常，放行原参数", exc_info=True)
        return fn_args, None
