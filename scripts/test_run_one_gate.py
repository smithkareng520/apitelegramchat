#!/usr/bin/env python3
"""run_one 分发闸门的执行层集成测试（TOOL_ARGS_PIPELINE.md 配套）。

模拟一条完整的工具批次执行路径，验证 L2 语义校验闸门在真实执行
咽喉（tool_call_loop._run_tool_calls_and_append → run_one）上的行为：

  1. 合法 JSON 但缺必填字段   → 不执行，可操作错误作为 tool 消息回传
  2. 字符串布尔（历史容忍写法）→ 容错矫正后执行
  3. strict 模式的 null 可选值  → 剥离后执行（executor 默认值语义保留）
  4. 畸形 JSON（诊断信封路径）  → 不执行，解析诊断回传
  5. 完全合法调用             → 正常执行

用法::

    PYTHONPATH=src python scripts/test_run_one_gate.py
"""
import asyncio
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from apitelegramchat.ai import tool_call_loop as tcl  # noqa: E402
from apitelegramchat.search_engine import SEARCH_TOOLS  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}  {detail}")


class FakeBuilder:
    """RichMessageBuilder 的最小桩：只实现 _run_tool_calls_and_append
    实际触达的接口。"""

    def __init__(self):
        self.chat_id = 12345
        self._tool_groups = [{"finished": True}]

    async def flush(self, force=False):
        return None

    def add_tool_item(self, *args, **kwargs):
        return None

    def update_tool_item(self, *args, **kwargs):
        return None

    def update_tool_preview(self, *args, **kwargs):
        return None

    def request_flush(self, force=False):
        return None

    def _get_current_group(self):
        return 0

    def finish_group(self, idx):
        if self._tool_groups:
            self._tool_groups[min(idx, len(self._tool_groups) - 1)]["finished"] = True


# ---- 替换执行层的外部依赖（只测闸门逻辑，不真正跑沙箱/格式化） ----
DISPATCHED = []


async def fake_dispatch(fn_name, fn_args, chat_id=None, progress_callback=None):
    DISPATCHED.append((fn_name, json.loads(json.dumps(fn_args, ensure_ascii=False))))
    return "Exit code: 0\nstdout preview: (executed)"


async def fake_format_tool_result(fn_name, fn_args, content):
    return f"{fn_name} completed", "<p>(formatted)</p>"


def fake_truncate(content, fn_name=None):
    return content if isinstance(content, str) else str(content)


def fake_condense(fn_name, fn_args, content):
    return content


tcl.dispatch_tool_call = fake_dispatch
tcl.format_tool_result = fake_format_tool_result
tcl._truncate_tool_result = fake_truncate
tcl.condense_for_model = fake_condense


async def run_batch(raw_arguments: str, tc_id: str):
    """跑一个单工具批次，返回 (是否执行, tool 消息 content, 分发参数)。"""
    DISPATCHED.clear()
    tool_calls = [{
        "id": tc_id, "type": "function",
        "function": {"name": "bash", "arguments": raw_arguments},
    }]
    loop_messages: list = []
    history: list = []
    status = await tcl._run_tool_calls_and_append(
        tool_calls, loop_messages, history, [0], "test", FakeBuilder(),
        chat_id=12345, tools=SEARCH_TOOLS,
    )
    tool_msgs = [m for m in loop_messages if m.get("role") == "tool"]
    content = tool_msgs[0]["content"] if tool_msgs else ""
    dispatched = dict(DISPATCHED[0][1]) if DISPATCHED else None
    return status, content, dispatched, tool_msgs


async def main():
    print("== run_one 分发闸门（L2 语义校验 + L1 修复 + L3 信封） ==")

    # 1) 合法 JSON 但缺必填 command → 拦截，错误回传
    status, content, dispatched, msgs = await run_batch(
        json.dumps({"_description": "列目录"}), "call_schema_miss")
    check("1a 缺必填 command：工具未执行", dispatched is None)
    check("1b 错误以 Error: 开头并指明 required",
          content.startswith("Error:") and "required" in content)
    check("1c 错误包含 How to fix 与参数清单",
          "[How to fix]" in content and "Required parameters" in content)
    check("1d 配对的 tool 消息已回写（供模型自纠）", len(msgs) == 1)
    check("1e 批次状态 continue（不炸循环）", status == "continue")

    # 2) 字符串布尔 → 容错矫正后执行
    status, content, dispatched, _ = await run_batch(
        json.dumps({"command": "ls -la", "_description": "x", "restart": "true"}),
        "call_coerce_bool")
    check("2a 字符串布尔被矫正为 true 后执行", dispatched is not None
          and dispatched.get("restart") is True, repr(dispatched))

    # 3) strict 模式 null 可选值 → 剥离后执行
    status, content, dispatched, _ = await run_batch(
        json.dumps({"command": "ls -la", "_description": "x", "restart": None}),
        "call_null_strip")
    check("3a null 可选值剥离后执行", dispatched is not None and "restart" not in dispatched,
          repr(dispatched))

    # 4) 畸形 JSON（字符串内未转义引号）→ 先修复；修复成功直接执行
    status, content, dispatched, _ = await run_batch(
        '{"command": "echo "hello world"", "_description": "x"}', "call_repair_exec")
    check("4a 未转义引号经 json-repair 修复后执行", dispatched is not None
          and "echo" in str(dispatched.get("command", "")), repr(dispatched))

    # 5) 截断 JSON → 拒绝执行，诊断信封回传
    status, content, dispatched, _ = await run_batch(
        '{"command": "rm -rf /tmp/ju', "call_truncated")
    check("5a 截断参数不执行（安全约束）", dispatched is None)
    check("5b 截断诊断回传（truncated + parser error）",
          content.startswith("Error:") and "truncated" in content.lower())

    # 6) 完全合法调用 → 正常执行
    status, content, dispatched, _ = await run_batch(
        json.dumps({"command": "echo ok", "_description": "x"}), "call_ok")
    check("6a 合法调用正常执行", dispatched is not None
          and dispatched.get("command") == "echo ok")

    # 7) 类型错误（command 为数字）→ 拦截并指出类型
    status, content, dispatched, _ = await run_batch(
        json.dumps({"command": 42}), "call_type")
    check("7a command 类型错误：不执行且错误指出 string",
          dispatched is None and "string" in content and "Error:" in content)

    print(f"\n通过 {PASS} 项 / 失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
