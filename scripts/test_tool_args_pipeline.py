#!/usr/bin/env python3
"""工具参数主流四层管线验证脚本（TOOL_ARGS_PIPELINE.md 配套）。

用法（项目根目录）::

    PYTHONPATH=src python scripts/test_tool_args_pipeline.py

依赖 json-repair / jsonschema 未安装时，相关分组自动退化为内置引擎
断言（与生产行为一致：可选依赖缺失 → 兜底引擎）。

覆盖分组：
  A. strict_tools   —— strict 注入 / 递归规范化 / 不变量（原始列表不
                       被修改）/ 网关拒绝降级 / 环境变量开关
  B. json_repair    —— 双引擎修复 / 截断安全 / 诊断信封 / 兜底引擎
  C. schema_validation —— null 剥离 / 写法容错 / 语义校验 / 错误消息
  D. 端到端          —— _normalize_tool_arguments 全链路回归
  E. 集成          —— 三条循环的 tools 透传与签名兼容
"""
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from apitelegramchat.ai.json_repair import (  # noqa: E402
    _INVALID_TOOL_ARGUMENTS_KEY,
    build_invalid_arguments_envelope,
    invalid_arguments_message,
    repair_json_arguments,
)
import apitelegramchat.ai.json_repair as jr  # noqa: E402
from apitelegramchat.ai.schema_validation import (  # noqa: E402
    coerce_common_slops,
    find_tool_schema,
    normalize_and_validate,
    strip_null_arguments,
    validate_tool_arguments,
)
import apitelegramchat.ai.schema_validation as sv  # noqa: E402
from apitelegramchat.ai.strict_tools import (  # noqa: E402
    looks_like_strict_tool_rejection,
    mark_strict_tools_rejected,
    reset_strict_rejection_state,
    strict_rejected_labels,
    strict_tools_for_request,
)
from apitelegramchat.search_engine import SEARCH_TOOLS  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        FAILURES.append((label, detail))
        print(f"  FAIL {label}  {detail}")


def _tool_by_name(name: str):
    for t in SEARCH_TOOLS:
        if t.get("function", {}).get("name") == name:
            return t
    return None


def section(title: str):
    print(f"\n== {title} ==")


# =========================================================================
print(f"引擎环境: json-repair 库={'可用' if jr._repair_with_library('{\"a\":1}') else '不可用(内置兜底)'}"
      f" / jsonschema={'可用' if sv._jsonschema_mod is not None else '不可用(内置兜底)'}")

# =========================================================================
section("A. strict_tools：strict 注入与递归规范化")

reset_strict_rejection_state()
_tools_snapshot = json.dumps(SEARCH_TOOLS, ensure_ascii=False, sort_keys=True)
_request_tools = strict_tools_for_request("agnes", SEARCH_TOOLS)
check("A1 返回新列表（未命中降级/开关时不返回原对象）", _request_tools is not SEARCH_TOOLS)
check("A2 原始 SEARCH_TOOLS 完全未被修改",
      json.dumps(SEARCH_TOOLS, ensure_ascii=False, sort_keys=True) == _tools_snapshot)

_req_bash = next((t for t in _request_tools
                  if t.get("function", {}).get("name") == "bash"), None)
check("A3 bash 获得 strict: true", bool(_req_bash and _req_bash["function"].get("strict") is True))
if _req_bash:
    _params = _req_bash["function"]["parameters"]
    _props = _params["properties"]
    check("A4 bash required 覆盖全部属性（strict 硬性要求）",
          set(_params["required"]) == set(_props.keys()))
    check("A5 bash additionalProperties=false（strict 硬性要求）",
          _params.get("additionalProperties") is False)
    check("A6 必填属性 command 不可空（type 仍为纯 string）",
          _props["command"]["type"] == "string")
    check("A7 可选属性变为可空（_description/restart → [T, null]）",
          _props["_description"]["type"] == ["string", "null"]
          and _props["restart"]["type"] == ["boolean", "null"])
    check("A8 strict 副本剥除了 default 关键字（OpenAI strict 不支持）",
          "default" not in json.dumps(_params))
    check("A9 strict 副本剥除了 input_examples（非 OpenAI function 字段）",
          "input_examples" not in json.dumps(_req_bash))

_req_te = next((t for t in _request_tools
                if t.get("function", {}).get("name") == "text_editor"), None)
check("A10 text_editor 获得 strict（含嵌套 items 规范化）",
      bool(_req_te and _req_te["function"].get("strict") is True))
if _req_te:
    _vr = _req_te["function"]["parameters"]["properties"]["view_range"]
    check("A11 数组属性 view_range 保留 minItems/maxItems 且 items 递归规范化",
          _vr.get("type") == ["array", "null"] and _vr.get("minItems") == 2
          and _vr["items"] == {"type": "integer"})

_req_ws = next((t for t in _request_tools
                if t.get("function", {}).get("name") == "web_search"), None)
check("A12 web_search（union type + 根 anyOf）不注入 strict（保守 per-tool）",
      bool(_req_ws) and not _req_ws.get("function", {}).get("strict"))
check("A13 web_search 原样透传（同一对象引用）", _req_ws is _tool_by_name("web_search"))

# 环境变量总开关
os.environ["DISABLE_STRICT_TOOL_SCHEMA"] = "1"
try:
    check("A14 DISABLE_STRICT_TOOL_SCHEMA=1 → 返回原列表",
          strict_tools_for_request("openrouter", SEARCH_TOOLS) is SEARCH_TOOLS)
finally:
    os.environ.pop("DISABLE_STRICT_TOOL_SCHEMA", None)

# 网关拒绝运行时降级
reset_strict_rejection_state()
mark_strict_tools_rejected("agnes", "400 tools strict unsupported")
check("A15 拒绝后同 label 返回原列表",
      strict_tools_for_request("agnes", SEARCH_TOOLS) is SEARCH_TOOLS)
check("A16 其他 label 不受影响",
      strict_tools_for_request("openrouter", SEARCH_TOOLS) is not SEARCH_TOOLS)
reset_strict_rejection_state()

check("A17 looks_like_strict_tool_rejection：命中 strict 报错",
      looks_like_strict_tool_rejection("Error code: 400 - tools.0.function strict is not supported"))
check("A18 looks_like_strict_tool_rejection：命中 additionalProperties 报错",
      looks_like_strict_tool_rejection("400 additionalProperties must be false"))
check("A19 looks_like_strict_tool_rejection：无关 400 不误判",
      not looks_like_strict_tool_rejection("Error code: 400 - invalid message role"))
check("A20 rejected labels 可查询/可重置",
      "agnes" not in strict_rejected_labels())

# =========================================================================
section("B. json_repair：双引擎修复与截断安全")

_rep, _info = repair_json_arguments("{'command': 'echo \"hi\"', 'restart': True}")
check("B1 单引号 + Python 字面量修复为 dict",
      _rep == {"command": 'echo "hi"', "restart": True})
check("B2 修复说明记录引擎来源", "repaired with the json-repair library" in _info["fixes"]
      or any("single" in f for f in _info["fixes"]))

_rep, _info = repair_json_arguments('{"command": "grep "todo" notes.txt", "timeout": 30}')
check("B3 日志同款：字符串内未转义双引号 → 修复成功",
      isinstance(_rep, dict) and "grep" in str(_rep.get("command", "")), repr(_rep))

_rep, _info = repair_json_arguments('{"command": "rm -rf /tmp/ju')
check("B4 截断参数拒绝猜测补全（执行路径安全约束）", _rep is None and _info["truncated"] is True)

_rep, _info = repair_json_arguments('{"command": "rm -rf /tmp/ju', allow_close_truncated=True)
check("B5 展示路径（allow_close）允许闭合截断", isinstance(_rep, dict))

_rep, _info = repair_json_arguments('{"a": "x\\ty", "b": [1, 2,]}')
check("B6 尾逗号删除", _rep == {"a": "x\ty", "b": [1, 2]})

_env = jr._LIB_REPAIR_JSON
jr._LIB_REPAIR_JSON = False
try:
    _rep, _info = repair_json_arguments("{'command': 'ls -la'}")
    check("B7 库不可用时兜底引擎仍可修复", _rep == {"command": "ls -la"})
finally:
    jr._LIB_REPAIR_JSON = _env

_envl = jr._LIB_REPAIR_JSON
jr._LIB_REPAIR_JSON = None
try:
    jr._repair_with_library("{bad")
    check("B8 库引擎对不可修复输入返回 None（不抛异常）", True)
except Exception:
    check("B8 库引擎对不可修复输入返回 None（不抛异常）", False)
finally:
    jr._LIB_REPAIR_JSON = _envl

# =========================================================================
section("C. schema_validation：语义校验层")

check("C1 strip_null_arguments 剥掉 null、保留 False/''/0",
      strip_null_arguments({"a": None, "b": False, "c": "", "d": 0, "e": "x"})
      == {"b": False, "c": "", "d": 0, "e": "x"})
_d = {"a": None}
strip_null_arguments(_d)
check("C2 strip_null_arguments 不修改入参", _d.get("a") is None)

_bash_schema = find_tool_schema("bash", SEARCH_TOOLS)
check("C3 find_tool_schema 找到 bash schema",
      isinstance(_bash_schema, dict) and "command" in _bash_schema.get("properties", {}))
check("C4 find_tool_schema 未知工具 → None", find_tool_schema("nope", SEARCH_TOOLS) is None)

_coerced = coerce_common_slops(
    {"restart": "true", "num": "30", "command": "ls", "note": "abc"},
    _bash_schema)
check("C5 字符串布尔按 schema 矫正", _coerced["restart"] is True)
check("C6 未声明字段不做矫正（num 保留字符串）", _coerced["num"] == "30")

_ws_schema = find_tool_schema("web_search", SEARCH_TOOLS)
_coerced = coerce_common_slops({"num_results": "5", "query": "x"}, _ws_schema)
check("C7 数字字符串按 integer 字段矫正", _coerced["num_results"] == 5)

_err = validate_tool_arguments("bash", {"_description": "ls"}, SEARCH_TOOLS)
check("C8 缺必填 command → 错误", _err is not None and "required" in _err)
check("C9 错误以 Error: 开头（连击熔断签名）", bool(_err and _err.startswith("Error:")))
check("C10 错误要求重发同一调用", bool(_err and "Reissue the SAME tool call" in _err))

_err = validate_tool_arguments("bash", {"command": 123}, SEARCH_TOOLS)
check("C11 command 类型错误 → 错误指出类型", _err is not None and "string" in _err)

_err = validate_tool_arguments("text_editor", {"command": "delete", "path": "a.txt"},
                               SEARCH_TOOLS)
check("C12 枚举外取值 → 错误", _err is not None and ("enum" in _err.lower() or "delete" in _err))

check("C13 未知额外键宽容（不惩罚）",
      validate_tool_arguments("bash", {"command": "ls", "_summary": "列目录"},
                              SEARCH_TOOLS) is None)

check("C14 合法调用通过",
      validate_tool_arguments("bash", {"command": "ls -la", "_description": "列目录"},
                              SEARCH_TOOLS) is None)

_err = validate_tool_arguments("web_search", {"num_results": 5}, SEARCH_TOOLS)
check("C15 web_search 缺 query/image_url 二选一 → 错误点名字段",
      _err is not None and "query" in _err and "image_url" in _err)

check("C16 web_search 带 query 通过",
      validate_tool_arguments("web_search", {"query": "test"}, SEARCH_TOOLS) is None)
check("C17 web_search 带 image_url 通过",
      validate_tool_arguments("web_search", {"image_url": "https://x/y.png"},
                              SEARCH_TOOLS) is None)

_norm, _err = normalize_and_validate(
    "bash", {"command": "ls", "restart": None, "_description": "x"}, SEARCH_TOOLS)
check("C18 normalize_and_validate：null 剥离 + 校验通过",
      _norm == {"command": "ls", "_description": "x"} and _err is None)

_norm, _err = normalize_and_validate("bash", {"restart": "true"}, SEARCH_TOOLS)
check("C19 normalize_and_validate：缺必填 → 错误且矫正已生效",
      _err is not None and "command" in _err and _norm.get("restart") is True)

check("C20 tools=None 时跳过校验（旧调用方兼容）",
      normalize_and_validate("bash", {"command": "ls"}, None) == ({"command": "ls"}, None))

# =========================================================================
section("D. 端到端：_normalize_tool_arguments 全链路回归")

from apitelegramchat.ai.tool_summary import _normalize_tool_arguments  # noqa: E402

_norm, _corrected, _meta = _normalize_tool_arguments(
    "{'command': 'ls -la', '_description': '列目录'}")
check("D1 畸形 JSON 修复路径：合法 JSON 输出", not _corrected and _meta["kind"] == "repaired")
_back = json.loads(_norm)
check("D2 修复后的参数保持可执行字段", _back.get("command") == "ls -la")

_norm, _corrected, _meta = _normalize_tool_arguments('{"command": "rm -rf /tmp/ju')
check("D3 截断 → 可恢复信封（不执行）", _corrected and _meta["kind"] == "invalid")
_env = json.loads(_norm)
check("D4 信封包含诊断键且本身是合法 JSON",
      _INVALID_TOOL_ARGUMENTS_KEY in _env and "parse_error" in _env)

_norm, _corrected, _meta = _normalize_tool_arguments('{"command": "ls"}')
check("D5 合法 JSON object → 直接重序列化", not _corrected and _meta["kind"] == "valid")

_norm, _corrected, _meta = _normalize_tool_arguments('[1, 2, 3]')
check("D6 顶层为数组 → 信封指出必须是 object",
      _corrected and "JSON object" in json.dumps(json.loads(_norm)))

_msg = invalid_arguments_message("bash", json.loads(_norm))
check("D7 错误消息以 Error: 开头", _msg.startswith("Error:"))

_envd = build_invalid_arguments_envelope('{"command": "grep "x" f"}')
check("D8 诊断信封可序列化为合法 JSON（回传不 400）",
      isinstance(json.loads(json.dumps(_envd, ensure_ascii=False)), dict))

# =========================================================================
section("E. 集成：三条循环的 tools 透传与签名兼容")

import inspect  # noqa: E402
from apitelegramchat.ai.tool_call_loop import _run_tool_calls_and_append  # noqa: E402
from apitelegramchat.ai import agentic_loops, anthropic_bridge  # noqa: E402

_sig = inspect.signature(_run_tool_calls_and_append)
check("E1 _run_tool_calls_and_append 增加 tools 参数（默认 None 向后兼容）",
      _sig.parameters.get("tools") is not None
      and _sig.parameters["tools"].default is None)

_al_src = inspect.getsource(agentic_loops._agentic_loop_openai_compat)
check("E2 OpenAI 兼容循环：请求使用 strict 化工具",
      "strict_tools_for_request" in _al_src and 'create_params["tools"] = request_tools' in _al_src)
check("E3 OpenAI 兼容循环：BadRequest 自动降级重试",
      "BadRequestError" in _al_src and "mark_strict_tools_rejected" in _al_src)
check("E4 OpenAI 兼容循环：把 tools 传给执行层",
      "tools=tools" in _al_src)

_gl_src = inspect.getsource(agentic_loops._agentic_loop_gemini_openai_compat)
check("E5 Gemini 循环：默认不注入 strict（环境变量实验开关）",
      "gemini_strict_env_enabled" in _gl_src)
check("E6 Gemini 循环：把 tools 传给执行层", "tools=tools" in _gl_src)

_ab_src = inspect.getsource(anthropic_bridge._agentic_loop_anthropic)
check("E7 Anthropic 循环：把 tools 传给执行层（L2 校验同样生效）",
      "tools=tools" in _ab_src)

_cl_src = inspect.getsource(_run_tool_calls_and_append)
check("E8 执行层：分发前调用 normalize_and_validate",
      "normalize_and_validate" in _cl_src and "schema_error" in _cl_src)

# =========================================================================
print(f"\n{'=' * 60}")
print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
if FAILURES:
    print("失败清单：")
    for label, detail in FAILURES:
        print(f"  - {label}: {detail}")
    sys.exit(1)
print("全部通过")
