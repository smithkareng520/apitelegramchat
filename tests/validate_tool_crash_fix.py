#!/usr/bin/env python3
"""
针对本次修复的独立回归测试：

修复前的 bug：当 format_tool_result()（或 _truncate_tool_result()）在处理某个
工具的原始输出时抛出未捕获异常，_run_tool_calls_and_append 会把该异常当作
"整个 run_one() 失败" 处理——只记一条 error log 就 continue，既不给这个
tool_call_id 补一条配对的 role=tool 消息，也不把 builder 里对应条目的状态从
"running" 推进到 done/error。

后果：
  1) 下一轮发给模型的 messages 里，assistant.tool_calls 引用的某个
     tool_call_id 找不到对应的 tool 消息 —— 多数 OpenAI 兼容网关会直接
     400，或者模型会陷入困惑/重试循环。
  2) 前端草稿里对应的折叠块永远停在"运行中"，用户看到的现象就是
     "一直刷新但没有新信息，后端却仍在跑"。

本测试直接调用真实的 _run_tool_calls_and_append，但把 dispatch_tool_call
和 format_tool_result 替换成受控的假实现：dispatch 正常返回结果，
format_tool_result 对其中一个工具调用故意抛出异常，模拟 "工具本身成功、
格式化阶段炸了" 的场景。断言修复后：
  - loop_messages / new_history_entries 中，每一个原始 tool_call 都有且
    仅有一条配对的 role=tool 消息（tool_call_id 一一对应，不多不少）；
  - builder 记录到的每个 tool_call_id 的最终状态都不是 "running"。
"""
import asyncio
import os
import sys
import types
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _stub_missing_third_party_modules() -> None:
    """
    沙箱环境无网络，部分第三方依赖（aiohttp / cachetools / openai / PIL）不可安装。
    这里只在它们确实缺失时才注入最小 stub，且只影响 sys.modules，不改动任何
    源码——目的仅仅是让 ai_handlers.py 能被 import 到，从而对本次修复的真实
    代码路径做端到端验证，而不是脱离源码单独重写一份逻辑来测试。
    """
    def ensure(name: str, build):
        try:
            __import__(name)
        except ImportError:
            sys.modules[name] = build()

    def build_aiohttp():
        mod = types.ModuleType("aiohttp")
        class ClientTimeout:
            def __init__(self, *a, **k): pass
        class ClientSession:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
        class _Err(Exception): pass
        mod.ClientTimeout = ClientTimeout
        mod.ClientSession = ClientSession
        mod.ClientConnectorError = _Err
        mod.ServerDisconnectedError = _Err
        mod.ClientOSError = _Err
        mod.ClientError = _Err
        return mod

    def build_cachetools():
        mod = types.ModuleType("cachetools")
        class TTLCache(dict):
            def __init__(self, maxsize=0, ttl=0): super().__init__()
        mod.TTLCache = TTLCache
        return mod

    def build_openai():
        mod = types.ModuleType("openai")
        class AsyncOpenAI:
            def __init__(self, *a, **k): pass
        mod.AsyncOpenAI = AsyncOpenAI
        return mod

    def build_pil():
        pil = types.ModuleType("PIL")
        image_mod = types.ModuleType("PIL.Image")
        class Image:
            pass
        image_mod.Image = Image
        pil.Image = image_mod
        sys.modules["PIL"] = pil
        return image_mod

    ensure("aiohttp", build_aiohttp)
    ensure("cachetools", build_cachetools)
    ensure("openai", build_openai)
    try:
        __import__("PIL")
        __import__("PIL.Image")
    except ImportError:
        build_pil()


_stub_missing_third_party_modules()

import apitelegramchat.ai_handlers as handlers  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class FakeBuilder:
    """最小化 RichMessageBuilder 替身，只记录我们关心的状态转移。"""

    def __init__(self) -> None:
        self.chat_id = 123
        self.item_status: dict[str, str] = {}
        self.flush_calls = 0
        self.finished_groups: list[int] = []

    def _get_current_group(self) -> int:
        return 0

    def add_tool_item(self, tool_id, *args, **kwargs) -> None:
        self.item_status[tool_id] = "running"

    def update_tool_item(self, tool_id, summary, details_html, status="done") -> None:
        self.item_status[tool_id] = status

    def update_tool_preview(self, *args, **kwargs) -> None:
        pass

    def finish_group(self, group_idx: int) -> None:
        self.finished_groups.append(group_idx)

    async def flush(self, force: bool = False) -> None:
        self.flush_calls += 1


async def main() -> None:
    ok_id = f"call_{uuid.uuid4().hex[:8]}"
    crash_id = f"call_{uuid.uuid4().hex[:8]}"

    tool_calls = [
        {"id": ok_id, "type": "function",
         "function": {"name": "weather", "arguments": "{}"}},
        {"id": crash_id, "type": "function",
         "function": {"name": "weather", "arguments": "{}"}},
    ]

    call_count = {"n": 0}

    async def fake_dispatch(name, arguments, chat_id, progress_callback=None):
        # dispatch 本身总是成功——问题不在工具执行，而在后续的格式化阶段。
        call_count["n"] += 1
        return "ok result"

    async def fake_format(fn_name, fn_args, result_str):
        # 让"第二个"工具调用在格式化阶段抛出未捕获异常，
        # 模拟 format_tool_result 内部一个未被 try/except 覆盖的 bug。
        if result_str == "ok result" and fake_format.calls == 1:
            fake_format.calls += 1
            raise IndexError("simulated formatter crash")
        fake_format.calls += 1
        return "ok summary", "<p>ok</p>"

    fake_format.calls = 0

    orig_dispatch = handlers.dispatch_tool_call
    orig_format = handlers.format_tool_result
    handlers.dispatch_tool_call = fake_dispatch
    handlers.format_tool_result = fake_format
    try:
        builder = FakeBuilder()
        loop_messages: list = []
        new_history_entries: list = []
        tool_call_count_ref = [0]

        result = await handlers._run_tool_calls_and_append(
            tool_calls=tool_calls,
            loop_messages=loop_messages,
            new_history_entries=new_history_entries,
            tool_call_count_ref=tool_call_count_ref,
            api_label="test",
            builder=builder,
            chat_id=123,
        )

        require(result != "over_limit", "不应触发工具调用预算上限")

        # 1) 每个原始 tool_call_id 都必须在 loop_messages 里有且仅有一条
        #    配对的 role=tool 消息。
        tool_msg_ids = [
            m["tool_call_id"] for m in loop_messages
            if isinstance(m, dict) and m.get("role") == "tool"
        ]
        for tc in tool_calls:
            tid = tc["id"]
            count = tool_msg_ids.count(tid)
            require(
                count == 1,
                f"tool_call_id={tid} 应该恰好有 1 条配对 tool 消息，实际={count}"
            )

        # 2) new_history_entries 必须与 loop_messages 中新增的 tool 消息一致
        #    （否则下一轮持久化历史时同样会出现配对缺失）。
        history_tool_ids = [
            m["tool_call_id"] for m in new_history_entries
            if isinstance(m, dict) and m.get("role") == "tool"
        ]
        require(
            sorted(history_tool_ids) == sorted(tool_msg_ids),
            "new_history_entries 与 loop_messages 中的 tool 消息应一致"
        )

        # 3) builder 中两个工具条目都不应停留在 "running"。
        require(
            ok_id in builder.item_status and builder.item_status[ok_id] != "running",
            f"正常工具 {ok_id} 的状态不应停在 running，实际={builder.item_status.get(ok_id)}"
        )
        require(
            crash_id in builder.item_status and builder.item_status[crash_id] != "running",
            f"格式化崩溃的工具 {crash_id} 的状态不应停在 running（这正是修复前的 bug），"
            f"实际={builder.item_status.get(crash_id)}"
        )
        # 注意：format_tool_result 崩溃已经在 run_one 内部被捕获并降级为纯文本展示
        # （见 ai_handlers.py 的第一层防护），所以这个工具调用本身仍然算"成功执行，
        # 只是格式化失败"，外层结果状态是 done 而不是 error——这才是期望行为：
        # 工具真的失败时才应该是 error，格式化 bug 不该冒充"工具失败"。
        require(
            builder.item_status[crash_id] == "done",
            f"格式化崩溃但工具本身成功时应标记为 done（已在 run_one 内部降级处理），"
            f"实际={builder.item_status.get(crash_id)}"
        )

        print("PASS (第一层防护): run_one 内部捕获了 format_tool_result 崩溃，"
              "降级为纯文本展示，两个工具调用都正常拿到配对 tool 消息且状态推进为 done。")
    finally:
        handlers.dispatch_tool_call = orig_dispatch
        handlers.format_tool_result = orig_format


async def main_outer_layer_crash() -> None:
    """
    第二层防护测试：模拟异常发生在 run_one() 内部try/except完全覆盖不到的
    位置——用一个在 __aexit__ 时抛异常的假 semaphore 替换
    handlers.tool_semaphore。这个异常发生在 run_one 的 `async with
    tool_semaphore:` 块退出时，在所有 try/except 范围之外，一定会原样
    冒泡到 asyncio.gather(..., return_exceptions=True)，从而真正触发外层
    isinstance(res, Exception) 的兜底分支（也就是本次修复的第二处改动）。

    验证：即使 run_one 整体作为一个 Task 失败，对应的 tool_call_id 依然会
    被外层循环兜底补上配对 tool 消息，builder 状态也会被推进（不停在
    running）。
    """
    ok_id = f"call_{uuid.uuid4().hex[:8]}"
    crash_id = f"call_{uuid.uuid4().hex[:8]}"

    tool_calls = [
        {"id": ok_id, "type": "function",
         "function": {"name": "weather", "arguments": "{}"}},
        {"id": crash_id, "type": "function",
         "function": {"name": "weather", "arguments": "{}"}},
    ]

    async def fake_dispatch(name, arguments, chat_id, progress_callback=None):
        return "ok result"

    async def fake_format(fn_name, fn_args, result_str):
        return "ok summary", "<p>ok</p>"

    real_semaphore = asyncio.Semaphore(1)

    class FlakySemaphore:
        """用真实 asyncio.Semaphore(1) 强制两次工具调用严格串行，从而保证
        "第一个 tool_call 在 __aexit__ 阶段崩溃" 这个场景是确定性可复现的，
        不受事件循环调度顺序影响。模拟的是一个 run_one 内部 try/except
        完全无法覆盖的崩溃点（真实场景可能是未来在 `async with
        tool_semaphore:` 退出路径上引入的任何 bug）。
        """

        def __init__(self, crash_tc_id: str):
            self._crash_tc_id = crash_tc_id
            self._entered_ids: list[str] = []

        def __call__(self):
            return self

        async def __aenter__(self):
            await real_semaphore.acquire()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            real_semaphore.release()
            self._entered_ids.append(self._crash_tc_id)
            if len(self._entered_ids) == 1:
                raise RuntimeError("simulated semaphore teardown crash")
            return False

    orig_dispatch = handlers.dispatch_tool_call
    orig_format = handlers.format_tool_result
    orig_semaphore = handlers.tool_semaphore
    handlers.dispatch_tool_call = fake_dispatch
    handlers.format_tool_result = fake_format
    handlers.tool_semaphore = FlakySemaphore(crash_tc_id=ok_id)
    try:
        builder = FakeBuilder()
        loop_messages: list = []
        new_history_entries: list = []
        tool_call_count_ref = [0]

        await handlers._run_tool_calls_and_append(
            tool_calls=tool_calls,
            loop_messages=loop_messages,
            new_history_entries=new_history_entries,
            tool_call_count_ref=tool_call_count_ref,
            api_label="test",
            builder=builder,
            chat_id=123,
        )

        tool_msg_ids = [
            m["tool_call_id"] for m in loop_messages
            if isinstance(m, dict) and m.get("role") == "tool"
        ]
        for tc in tool_calls:
            tid = tc["id"]
            count = tool_msg_ids.count(tid)
            require(
                count == 1,
                f"tool_call_id={tid} 应该恰好有 1 条配对 tool 消息，实际={count}"
            )

        require(
            builder.item_status.get(ok_id) == "error",
            f"run_one 整体崩溃（内层 try/except 覆盖不到的异常）时，外层兜底应把状态"
            f"标记为 error 而不是停在 running，实际={builder.item_status.get(ok_id)}"
        )
        require(
            builder.item_status.get(crash_id) not in (None, "running"),
            f"未受影响的第二个工具状态不应为 running，实际={builder.item_status.get(crash_id)}"
        )

        print("PASS (第二层防护 / run_one 整体崩溃场景): 即使异常发生在 run_one "
              "所有 try/except 范围之外、直接冒泡给 asyncio.gather，外层兜底依然"
              "补齐了配对 tool 消息，并把该工具的 UI 状态从 running 推进为 error——"
              "不会再出现折叠块永远转圈、模型消息历史缺 tool 配对的情况。")
    finally:
        handlers.dispatch_tool_call = orig_dispatch
        handlers.format_tool_result = orig_format
        handlers.tool_semaphore = orig_semaphore


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(main_outer_layer_crash())
