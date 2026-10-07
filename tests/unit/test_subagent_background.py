# tests/unit/test_subagent_background.py — 后台子 agent（启动→句柄→查询/停止）
# 覆盖 subagent_background 的核心语义（execute_subagent 用桩替换，不发真实 LLM 请求）：
#   - 启动立即返回句柄，完成通知入 bash_background 共用的待送队列；
#   - stop 不重复通知；失败 / 超时终态映射；每 chat 运行数上限；
#   - 父回合取消不波及 runner；应用关闭取消全部且不推通知；
#   - dispatch 路由（task_action → run_in_background → 前台）、schema 与卡片渲染。

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("openai", reason="subagent tests require the project runtime dependency: openai")

import bash_background
import subagent_background
import subagent_tool
import workspace_paths

CHAT = 9001
NS = "ns-subagent-bg"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    workspace_paths.data_root.cache_clear()
    workspace_paths.workspaces_root.cache_clear()
    subagent_background._TASKS.clear()
    bash_background._TASKS.clear()
    bash_background._LOADED_KEYS.clear()
    bash_background._PENDING_NOTICES.clear()
    yield
    subagent_background._TASKS.clear()
    bash_background._PENDING_NOTICES.clear()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _ok_payload(answer: str = "<b>结论</b>") -> str:
    return json.dumps({"ok": True, "rounds": 3, "tool_calls": 5, "answer": answer, "elapsed": 1.0})


def _patch_execute(monkeypatch: Any, fn: Any) -> None:
    monkeypatch.setattr(subagent_tool, "execute_subagent", fn)


async def _wait_done(task: "subagent_background.SubagentTask", timeout: float = 5.0) -> None:
    assert task.runner is not None
    await asyncio.wait_for(asyncio.shield(task.runner), timeout=timeout)


def _only_task() -> "subagent_background.SubagentTask":
    (task,) = subagent_background._TASKS[(CHAT, NS)].values()
    return task


# 启动 / 完成 / 通知
def test_start_returns_handle_then_notice_is_queued(monkeypatch: Any) -> None:
    async def fake(**kw: Any) -> str:
        await kw["progress_callback"]("第 1/32 轮：LLM 思考中…（已耗时 1s）")
        await asyncio.sleep(0.05)
        return _ok_payload()

    _patch_execute(monkeypatch, fake)

    async def scenario() -> None:
        handle = await subagent_background.start_background_task(
            CHAT, NS, task="调研 X", description="X 调研")
        first = handle.splitlines()[0]
        assert first.startswith("✅ 后台子 agent 已启动 sa-") and "X 调研" in first
        task = _only_task()
        assert task.status == "running"
        # 启动立即返回：此刻还没有完成通知
        assert bash_background.drain_completion_notices(CHAT, NS) == []
        await _wait_done(task)
        assert task.status == "done" and task.progress.startswith("第 1/32 轮")
        (notice,) = bash_background.drain_completion_notices(CHAT, NS)
        assert notice.splitlines()[0].startswith("✅ 后台子 agent " + task.task_id)
        assert "<b>结论</b>" in notice and f"task_id={task.task_id}" in notice
        # 发完即消费
        assert bash_background.drain_completion_notices(CHAT, NS) == []

    _run(scenario())


def test_failure_and_timeout_status_mapping(monkeypatch: Any) -> None:
    results = iter([
        json.dumps({"ok": False, "error": "LLM 调用失败: boom", "rounds": 1, "tool_calls": 0}),
        json.dumps({"ok": False, "error": "子 agent 整体超时（60s）", "code": "timeout"}),
        "not-json",
    ])

    async def fake(**_kw: Any) -> str:
        return next(results)

    _patch_execute(monkeypatch, fake)

    async def scenario() -> None:
        statuses = []
        for _ in range(3):
            await subagent_background.start_background_task(CHAT, NS, task="t")
            task = list(subagent_background._TASKS[(CHAT, NS)].values())[-1]
            await _wait_done(task)
            statuses.append(task.status)
        assert statuses == ["failed", "expired", "failed"]
        notices = bash_background.drain_completion_notices(CHAT, NS)
        assert [n[0] for n in notices] == ["❌", "⌛", "❌"]

    _run(scenario())


def test_empty_task_is_rejected_without_creating_a_task() -> None:
    out = _run(subagent_background.start_background_task(CHAT, NS, task="   "))
    assert out.startswith("Error:") and not subagent_background._TASKS.get((CHAT, NS))


def test_per_chat_running_limit(monkeypatch: Any) -> None:
    gate = asyncio.Event

    async def scenario() -> None:
        release = gate()

        async def fake(**_kw: Any) -> str:
            await release.wait()
            return _ok_payload()

        _patch_execute(monkeypatch, fake)
        for _ in range(subagent_background.SUBAGENT_BG_MAX_PER_CHAT):
            assert (await subagent_background.start_background_task(CHAT, NS, task="t")).startswith("✅")
        over = await subagent_background.start_background_task(CHAT, NS, task="t")
        assert over.startswith("Error: 后台子 agent 数量已达上限")
        release.set()
        for t in list(subagent_background._TASKS[(CHAT, NS)].values()):
            await _wait_done(t)
        # 任务结束后可再次启动
        assert (await subagent_background.start_background_task(CHAT, NS, task="t")).startswith("✅")
        await subagent_background.shutdown_all()

    _run(scenario())


# 查询 / 停止
def test_status_output_list_and_unknown_id(monkeypatch: Any) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def fake(**kw: Any) -> str:
            await kw["progress_callback"]("第 2/32 轮：执行工具 web_search…（已耗时 3s）")
            await release.wait()
            return _ok_payload("完整答复")

        _patch_execute(monkeypatch, fake)
        await subagent_background.start_background_task(CHAT, NS, task="t")
        task = _only_task()
        await asyncio.sleep(0.02)

        running_status = await subagent_background.query_task(CHAT, NS, "status", task.task_id)
        assert running_status.startswith("⏳") and "web_search" in running_status
        assert "仍在运行" in await subagent_background.query_task(CHAT, NS, "output", task.task_id)
        assert "运行中 1 / 共 1" in await subagent_background.query_task(CHAT, NS, "list")

        release.set()
        await _wait_done(task)
        assert (await subagent_background.query_task(CHAT, NS, "status", task.task_id)).startswith("✅")
        out = await subagent_background.query_task(CHAT, NS, "output", task.task_id)
        assert out.startswith("📜") and "完整答复" in out

        assert (await subagent_background.query_task(CHAT, NS, "status", "sa-deadbeef")).startswith("Error: 未找到任务")
        assert (await subagent_background.query_task(CHAT, NS, "status")).startswith("Error:")
        assert (await subagent_background.query_task(CHAT, NS, "bogus")).startswith("Error: 未知 task_action")

    _run(scenario())


def test_stop_cancels_runner_without_notice(monkeypatch: Any) -> None:
    async def scenario() -> None:
        async def fake(**_kw: Any) -> str:
            await asyncio.sleep(60)
            return _ok_payload()

        _patch_execute(monkeypatch, fake)
        await subagent_background.start_background_task(CHAT, NS, task="t")
        task = _only_task()
        await asyncio.sleep(0.02)
        msg = await subagent_background.query_task(CHAT, NS, "stop", task.task_id)
        assert msg.startswith("⏹") and task.status == "stopped"
        assert task.runner is not None and task.runner.done()
        # 主动 stop 结果已同步返回，不再入队通知
        assert bash_background.drain_completion_notices(CHAT, NS) == []
        again = await subagent_background.query_task(CHAT, NS, "stop", task.task_id)
        assert again.startswith("ℹ️")

    _run(scenario())


# 防误杀 / 关闭 / 回收
def test_parent_turn_cancellation_does_not_kill_background_runner(monkeypatch: Any) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def fake(**_kw: Any) -> str:
            await release.wait()
            return _ok_payload()

        _patch_execute(monkeypatch, fake)
        turn = asyncio.create_task(subagent_background.start_background_task(CHAT, NS, task="t"))
        await turn  # 启动调用已返回
        parent = asyncio.create_task(asyncio.sleep(60))  # 模拟仍在运行的父回合
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await parent
        task = _only_task()
        assert task.status == "running" and not (task.runner and task.runner.done())
        release.set()
        await _wait_done(task)
        assert task.status == "done"

    _run(scenario())


def test_shutdown_all_cancels_without_notice(monkeypatch: Any) -> None:
    async def scenario() -> None:
        async def fake(**_kw: Any) -> str:
            await asyncio.sleep(60)
            return _ok_payload()

        _patch_execute(monkeypatch, fake)
        await subagent_background.start_background_task(CHAT, NS, task="t")
        runner = _only_task().runner
        await subagent_background.shutdown_all()
        assert runner is not None and runner.done()
        assert not subagent_background._TASKS
        assert bash_background.drain_completion_notices(CHAT, NS) == []

    _run(scenario())


def test_finished_tasks_are_pruned(monkeypatch: Any) -> None:
    monkeypatch.setattr(subagent_background, "SUBAGENT_BG_FINISHED_RETENTION", 2)

    async def fake(**_kw: Any) -> str:
        return _ok_payload()

    _patch_execute(monkeypatch, fake)

    async def scenario() -> None:
        for _ in range(4):
            await subagent_background.start_background_task(CHAT, NS, task="t")
            await _wait_done(list(subagent_background._TASKS[(CHAT, NS)].values())[-1])
        assert len(subagent_background._TASKS[(CHAT, NS)]) == 2

    _run(scenario())


def test_long_answer_is_truncated_in_notice(monkeypatch: Any) -> None:
    async def fake(**_kw: Any) -> str:
        return _ok_payload("字" * 10_000)

    _patch_execute(monkeypatch, fake)

    async def scenario() -> None:
        await subagent_background.start_background_task(CHAT, NS, task="t")
        await _wait_done(_only_task())
        (notice,) = bash_background.drain_completion_notices(CHAT, NS)
        assert len(notice) < subagent_background.NOTICE_ANSWER_MAX_CHARS + 500 and "已截断" in notice

    _run(scenario())


# dispatch 路由 / schema / 卡片
def test_dispatch_routes_background_and_actions(monkeypatch: Any) -> None:
    import tool_dispatch

    calls: list[tuple[str, dict]] = []

    async def fake_start(chat_id: int, ns: str, **kw: Any) -> str:
        calls.append(("start", {"chat_id": chat_id, "ns": ns, **kw}))
        return "started"

    async def fake_query(chat_id: int, ns: str, action: str, task_id: Any = None) -> str:
        calls.append(("query", {"action": action, "task_id": task_id}))
        return "queried"

    async def fake_foreground(**kw: Any) -> str:
        calls.append(("foreground", kw))
        return "fg"

    monkeypatch.setattr(subagent_background, "start_background_task", fake_start)
    monkeypatch.setattr(subagent_background, "query_task", fake_query)
    _patch_execute(monkeypatch, fake_foreground)

    async def scenario() -> None:
        cb = object()
        assert await tool_dispatch._handle_subagent(
            CHAT, {"task": "t", "run_in_background": True, "description": "d", "timeout": 120}, NS, cb) == "started"
        assert await tool_dispatch._handle_subagent(
            CHAT, {"task_action": "status", "task_id": "sa-1", "run_in_background": True}, NS, cb) == "queried"
        assert await tool_dispatch._handle_subagent(CHAT, {"task": "t"}, NS, cb) == "fg"

    _run(scenario())
    (kind1, a1), (kind2, a2), (kind3, a3) = calls
    assert kind1 == "start" and a1["ns"] == NS and a1["task"] == "t" and a1["description"] == "d" and a1["timeout"] == 120
    # task_action 优先于 run_in_background（与 bash 一致）
    assert kind2 == "query" and a2 == {"action": "status", "task_id": "sa-1"}
    assert kind3 == "foreground" and a3["task"] == "t" and a3["progress_callback"] is not None


def test_schema_exposes_background_fields_and_no_required_task() -> None:
    params = subagent_tool.SUBAGENT_TOOL["function"]["parameters"]
    props = params["properties"]
    for key in ("run_in_background", "task_action", "task_id", "description"):
        assert key in props
    assert props["task_action"]["enum"] == ["status", "output", "stop", "list"]
    assert params["required"] == []
    # 前台语义不变：缺 task 且无 task_action 仍由执行层报 empty_task
    out = json.loads(_run(subagent_tool.execute_subagent(chat_id=CHAT, task="")))
    assert out["ok"] is False and out["code"] == "empty_task"


class _CharEncoding:
    """按字符计数的假编码：避免测试依赖 tiktoken 在线下载编码表。"""

    def encode(self, text: str, **_kw: Any) -> list[int]:
        return [ord(c) for c in text]

    def decode(self, tokens: list[int]) -> str:
        return "".join(chr(t) for t in tokens)


def test_background_results_render_as_cards_and_pass_condense(monkeypatch: Any) -> None:
    import token_budget

    monkeypatch.setattr(token_budget, "_get_encoding", lambda _name: _CharEncoding())
    from tool_result_condense import condense_for_model
    from tool_result_format import format_tool_result

    text = "✅ 后台子 agent 已启动 sa-12345678「X 调研」\n任务：调研 X"
    summary, details = _run(format_tool_result("subagent", {"run_in_background": True, "task": "t"}, text))
    assert summary.startswith("✅ 后台子 agent 已启动 sa-12345678") and "调研 X" in details
    summary2, _ = _run(format_tool_result("subagent", {"task_action": "list"}, "📋 后台子 agent 列表（运行中 0 / 共 0）：\n（无任务）"))
    assert summary2.startswith("📋")
    # 前台结果仍走原 JSON 卡片
    fg_summary, _ = _run(format_tool_result("subagent", {"task": "t"}, _ok_payload()))
    assert fg_summary.startswith("🤖")
    # 模型视图：纯文本结果原样通过
    assert condense_for_model("subagent", {"run_in_background": True}, text) == text
