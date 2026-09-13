# =====================================================================
# tests/unit/test_bash_background.py — bash 后台任务（启动→句柄→查询/停止）
# =====================================================================
# 覆盖 bash_background 模块的核心语义：
#   - 启动立即返回句柄（回合内不阻塞），完成通知入队（「就近搭车」）；
#   - 寿命上限到期强制终止；stop 优雅终止；每 chat 运行数上限；
#   - 隔离性：前台会话 idle 超时 killpg 不波及后台任务（防误杀核心）；
#   - 应用重启恢复：孤儿探活 / 退出码未知（lost）+ 补推通知；
#   - dispatch / execute_bash 路由转发；UI 卡片分支；L2 schema 校验。
#
# 进程类用例真实 spawn bash：沿用 test_bash_idle_timeout.py 的隔离方式
# （monkeypatch _apply_landlock 直通 + tmp 目录 + 根缓存 cache_clear）。
# preexec_fn 仅 POSIX 可用 → Windows 上跳过进程类用例（纯逻辑用例照跑）。
# =====================================================================

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

import bash_background
import sandbox
import workspace_paths
from bash_background import (
    _TASK_ID_RE,
    _command_is_safe,
    drain_completion_notices,
    query_task,
    start_background_task,
)
from bash_session import BashSession, execute_bash

_POSIX = sys.platform != "win32"
requires_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="preexec_fn / bash 仅 POSIX 可用"
)


# ---------------------------------------------------------------------------
# 隔离夹具
# ---------------------------------------------------------------------------
def _isolate(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(sandbox, "_apply_landlock", lambda path: True)
    workspace_paths.data_root.cache_clear()
    workspace_paths.workspaces_root.cache_clear()


def _reset_registries() -> None:
    bash_background._TASKS.clear()
    bash_background._LOADED_KEYS.clear()
    bash_background._PENDING_NOTICES.clear()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _shutdown_all_and_reset() -> None:
    await bash_background.shutdown_all()
    _reset_registries()


async def _wait_terminal(task: "bash_background.BackgroundTask", timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while task.status == "running" and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
    assert task.status != "running", "任务未在限时内进入终态"


# ---------------------------------------------------------------------------
# 纯逻辑：危险命令黑名单（模块级抽出后与前台会话共用）
# ---------------------------------------------------------------------------
def test_command_is_safe_rejects_disasters() -> None:
    assert not _command_is_safe("")
    assert not _command_is_safe("   ")
    assert not _command_is_safe(':(){ :|:& };:')
    assert not _command_is_safe("rm -rf /")
    assert not _command_is_safe("dd if=/dev/zero of=/dev/sda")


def test_command_is_safe_allows_normal_commands() -> None:
    assert _command_is_safe("pip install -v requests")
    assert _command_is_safe("echo hi && sleep 1")
    assert _command_is_safe("dd if=/dev/zero of=/dev/null count=1")


# ---------------------------------------------------------------------------
# 纯逻辑：UI 卡片分支（tool_result_format）
# ---------------------------------------------------------------------------
def test_is_background_bash_call_detection() -> None:
    from tool_result_format import _format_background_bash_result, _is_background_bash_call

    assert _is_background_bash_call({"run_in_background": True, "command": "ls"})
    assert _is_background_bash_call({"task_action": "status", "task_id": "bg-12345678"})
    assert not _is_background_bash_call({"command": "ls"})
    assert not _is_background_bash_call({"task_id": "bg-12345678"})
    assert not _is_background_bash_call("not-a-dict")
    # 非后台调用 → None（落回通用信封渲染）
    assert _format_background_bash_result({"command": "ls"}, "/ws$ ls\nExit code: 0") is None


def test_format_background_bash_result_card() -> None:
    from tool_result_format import format_tool_result

    handle = (
        "✅ 后台任务已启动 bg-1a2b3c4d「安装依赖」\n"
        "命令：pip install -v requests\n"
        "日志：/ws/.runtime/tasks/bg-1a2b3c4d.log"
    )
    summary, details = _run(format_tool_result(
        "bash", {"description": "安装依赖", "run_in_background": True, "command": "pip install"}, handle))
    assert "后台任务已启动" in summary and "bg-1a2b3c4d" in summary
    assert "bg-1a2b3c4d" in details  # 全文进入引用块

    status_text = "✅ 后台任务 bg-1a2b3c4d 已结束：exit code 0，用时 1m02s"
    summary2, _ = _run(format_tool_result(
        "bash", {"description": "查状态", "task_action": "status", "task_id": "bg-1a2b3c4d"}, status_text))
    assert "exit code 0" in summary2


# ---------------------------------------------------------------------------
# 纯逻辑：L2 schema 校验接受新参数
# ---------------------------------------------------------------------------
def test_l2_schema_accepts_background_params(tmp_path: Any, monkeypatch: Any) -> None:
    from ai.schema_validation import normalize_and_validate
    from search.tool_schemas import SEARCH_TOOLS

    _isolate(tmp_path, monkeypatch)
    fn_args, err = normalize_and_validate(
        "bash",
        {"description": "安装依赖", "command": "pip install -v requests", "run_in_background": True},
        SEARCH_TOOLS,
    )
    assert err is None
    assert fn_args["run_in_background"] is True

    fn_args2, err2 = normalize_and_validate(
        "bash",
        {"description": "查状态", "task_action": "status", "task_id": "bg-1a2b3c4d"},
        SEARCH_TOOLS,
    )
    assert err2 is None
    assert fn_args2["task_action"] == "status"

    # 非法 enum 被拒绝；缺 description 被拒绝
    _, err3 = normalize_and_validate(
        "bash", {"description": "x", "task_action": "destroy", "task_id": "bg-1a2b3c4d"}, SEARCH_TOOLS)
    assert err3
    _, err4 = normalize_and_validate("bash", {"command": "ls"}, SEARCH_TOOLS)
    assert err4


# ---------------------------------------------------------------------------
# 纯逻辑：通知队列 push/drain 与上限
# ---------------------------------------------------------------------------
def test_notice_queue_drain_and_cap(tmp_path: Any, monkeypatch: Any) -> None:
    _isolate(tmp_path, monkeypatch)
    _reset_registries()
    try:
        chat = 990001
        ns = workspace_paths.workspace_namespace(chat)
        for i in range(bash_background._PENDING_NOTICES_MAX_PER_CHAT + 5):
            bash_background.push_completion_notice(chat, ns, f"notice-{i}")
        # 上限截断：丢最旧（drain 为同步整段换空，无需事件循环）
        notices = drain_completion_notices(chat)
        assert len(notices) == bash_background._PENDING_NOTICES_MAX_PER_CHAT
        assert notices[0] == "notice-5"
        assert notices[-1] == f"notice-{bash_background._PENDING_NOTICES_MAX_PER_CHAT + 4}"
        # drain 后清空
        assert drain_completion_notices(chat) == []
    finally:
        _reset_registries()


# ---------------------------------------------------------------------------
# 进程集成（POSIX）
# ---------------------------------------------------------------------------
@requires_posix
def test_start_returns_handle_and_completes_with_notice(tmp_path: Any, monkeypatch: Any) -> None:
    """启动立即返回句柄；完成后通知入队（含输出尾部与退出码）；.exit 落盘。"""
    _isolate(tmp_path, monkeypatch)
    _reset_registries()

    async def scenario() -> None:
        try:
            chat = 990101
            handle = await start_background_task(
                chat, str(chat), "echo bg-done-marker-990101 && echo tail-line", description="打印标记")
            assert handle.startswith("✅ 后台任务已启动"), handle
            assert "task_action=status" in handle and "task_action=stop" in handle
            task_id = _TASK_ID_RE.search(handle).group(0)

            task = bash_background._TASKS[(chat, str(chat))][task_id]
            # 启动即返回：句柄生成时进程可能尚未退出，但任务必然已注册
            assert task.status == "running"
            await _wait_terminal(task)
            assert task.status == "done" and task.exit_code == 0

            # 完成通知入队（就近搭车）：含输出尾部与退出码；drain 后清空
            notices = drain_completion_notices(chat, str(chat))
            assert len(notices) == 1
            assert "bg-done-marker-990101" in notices[0]
            assert "exit code 0" in notices[0]
            assert drain_completion_notices(chat, str(chat)) == []

            # 终态磁盘标记
            assert Path(task.state_path).with_suffix(".exit").read_text(encoding="utf-8") == "done"
        finally:
            await _shutdown_all_and_reset()

    _run(scenario())


@requires_posix
def test_lifetime_cap_kills_long_task(tmp_path: Any, monkeypatch: Any) -> None:
    """超过寿命上限的任务被强制终止并标记 expired，通知说明原因。"""
    _isolate(tmp_path, monkeypatch)
    _reset_registries()
    monkeypatch.setattr(bash_background, "BASH_TASK_MAX_LIFETIME_SEC", 2)

    async def scenario() -> None:
        try:
            chat = 990102
            handle = await start_background_task(chat, str(chat), "sleep 60", description="长任务")
            task_id = _TASK_ID_RE.search(handle).group(0)
            task = bash_background._TASKS[(chat, str(chat))][task_id]
            await _wait_terminal(task, timeout=15.0)
            assert task.status == "expired"
            assert task.proc is None or task.proc.returncode is not None

            notices = drain_completion_notices(chat, str(chat))
            assert len(notices) == 1
            assert "寿命上限" in notices[0]
        finally:
            await _shutdown_all_and_reset()

    _run(scenario())


@requires_posix
def test_stop_task_gracefully(tmp_path: Any, monkeypatch: Any) -> None:
    """stop 走 SIGTERM 优雅终止；进程死亡、状态 stopped、无通知（模型同步得知）。"""
    _isolate(tmp_path, monkeypatch)
    _reset_registries()

    async def scenario() -> None:
        try:
            chat = 990103
            handle = await start_background_task(chat, str(chat), "sleep 60", description="待停任务")
            task_id = _TASK_ID_RE.search(handle).group(0)
            task = bash_background._TASKS[(chat, str(chat))][task_id]
            await asyncio.sleep(0.3)  # 让进程起来

            result = await query_task(chat, str(chat), "stop", task_id=task_id)
            assert "已停止" in result
            assert task.status == "stopped"
            assert task.proc is None or task.proc.returncode is not None
            # 模型主动 stop 同步得到结果 → 不重复入队
            assert drain_completion_notices(chat, str(chat)) == []

            # 重复 stop → 已结束提示
            again = await query_task(chat, str(chat), "stop", task_id=task_id)
            assert "已于先前结束" in again
        finally:
            await _shutdown_all_and_reset()

    _run(scenario())


@requires_posix
def test_per_chat_running_limit(tmp_path: Any, monkeypatch: Any) -> None:
    """每 chat 运行中任务数达到上限后拒绝新启动，错误列出现存任务。"""
    _isolate(tmp_path, monkeypatch)
    _reset_registries()
    monkeypatch.setattr(bash_background, "BASH_TASK_MAX_PER_CHAT", 1)

    async def scenario() -> None:
        try:
            chat = 990104
            handle1 = await start_background_task(chat, str(chat), "sleep 60", description="占位任务")
            assert handle1.startswith("✅")
            task_id1 = _TASK_ID_RE.search(handle1).group(0)

            handle2 = await start_background_task(chat, str(chat), "sleep 60", description="第二个")
            assert handle2.startswith("Error:") and "上限" in handle2
            assert task_id1 in handle2

            # stop 后即可再启动
            await query_task(chat, str(chat), "stop", task_id=task_id1)
            handle3 = await start_background_task(chat, str(chat), "echo ok", description="再启动")
            assert handle3.startswith("✅")
        finally:
            await _shutdown_all_and_reset()

    _run(scenario())


@requires_posix
def test_background_survives_session_idle_timeout(tmp_path: Any, monkeypatch: Any) -> None:
    """防误杀核心：前台会话 idle 超时 killpg 杀掉会话进程组，后台任务不受波及。"""
    _isolate(tmp_path, monkeypatch)
    _reset_registries()

    async def scenario() -> None:
        try:
            chat = 990105
            # 后台任务先启动（sleep 0.5 后输出标记），随后前台命令静默挂起
            handle = await start_background_task(
                chat, str(chat), "sleep 0.5 && echo bg-survivor-marker", description="幸存任务")
            task_id = _TASK_ID_RE.search(handle).group(0)
            task = bash_background._TASKS[(chat, str(chat))][task_id]

            session = BashSession(chat)
            try:
                out = await session.execute("sleep 999", total_timeout=300, idle_timeout=1)
                assert "idle limit" in out or "no output for" in out
                # 会话已被杀重启
                assert session.proc is None or session.proc.returncode is not None
            finally:
                await session.close()

            await _wait_terminal(task)
            assert task.status == "done" and task.exit_code == 0
            notices = drain_completion_notices(chat, str(chat))
            assert len(notices) == 1 and "bg-survivor-marker" in notices[0]
        finally:
            await _shutdown_all_and_reset()

    _run(scenario())


@requires_posix
def test_restart_registry_rediscovery(tmp_path: Any, monkeypatch: Any) -> None:
    """应用重启恢复：注册表清空后懒加载扫描 tasks 目录。
    - 进程已死且无 .exit → 状态 lost + 补推「退出码未知」通知；
    - 进程仍活 → 重新纳入探活（status 查询可见运行中），仍可 stop。"""
    _isolate(tmp_path, monkeypatch)
    _reset_registries()

    async def scenario() -> None:
        try:
            chat = 990106
            # 孤儿 A：启动后模拟应用重启（清注册表、取消 monitor），进程仍活着
            handle_a = await start_background_task(chat, str(chat), "sleep 60", description="孤儿存活")
            task_id_a = _TASK_ID_RE.search(handle_a).group(0)
            task_a = bash_background._TASKS[(chat, str(chat))][task_id_a]
            if task_a.monitor:
                task_a.monitor.cancel()
            bash_background._TASKS.clear()
            bash_background._LOADED_KEYS.clear()
            # 模拟进程重启：_PENDING_NOTICES 是纯内存态，随旧进程一同消失
            bash_background._PENDING_NOTICES.clear()
            await asyncio.sleep(0.2)  # 让进程稳定处于存活态

            # 孤儿 B：正常退出（快速 echo）后模拟重启 → 无 .exit、进程已死 → lost
            handle_b = await start_background_task(chat, str(chat), "echo quick-done", description="孤儿已亡")
            task_id_b = _TASK_ID_RE.search(handle_b).group(0)
            task_b = bash_background._TASKS[(chat, str(chat))][task_id_b]
            await _wait_terminal(task_b)  # 真实 monitor 记录 done 终态
            exit_marker_b = Path(task_b.state_path).with_suffix(".exit")
            assert exit_marker_b.exists()
            exit_marker_b.unlink()  # 模拟「重启期间结束、终态未记录」
            task_b.status = "running"  # 复原为重启前的磁盘视角
            bash_background._dump_state(task_b)
            bash_background._TASKS.clear()
            bash_background._LOADED_KEYS.clear()
            # 同上：重启后内存通知队列为空（旧进程里的 done 通知并未送达，
            # 也永不送达——这正是懒加载补推 lost 通知要掉的事）
            bash_background._PENDING_NOTICES.clear()

            # 懒加载触发（drain 内部）：孤儿 A 应被重新纳入探活并可见
            listing = await query_task(chat, str(chat), "list")
            assert task_id_a in listing and "运行中" in listing

            # 孤儿 B → lost + 补推通知
            notices = drain_completion_notices(chat, str(chat))
            lost_notices = [n for n in notices if task_id_b in n]
            assert lost_notices, f"未找到孤儿 B 的补推通知: {notices}"
            assert "退出码未知" in lost_notices[0]

            # 孤儿 A 仍可 stop（身份校验通过 → 信号送达）
            stop_result = await query_task(chat, str(chat), "stop", task_id=task_id_a)
            assert "已停止" in stop_result
        finally:
            await _shutdown_all_and_reset()

    _run(scenario())


@requires_posix
def test_execute_bash_routing(tmp_path: Any, monkeypatch: Any) -> None:
    """execute_bash 路由：task_action=list 走查询；后台模式缺 command 报错。"""
    _isolate(tmp_path, monkeypatch)
    _reset_registries()

    async def scenario() -> None:
        try:
            chat = 990107
            listing = await execute_bash(chat, task_action="list", namespace=str(chat))
            assert "后台任务列表" in listing and "无任务" in listing

            missing = await execute_bash(
                chat, run_in_background=True, namespace=str(chat))
            assert missing.startswith("Error:") and "command is required" in missing

            bad_action = await execute_bash(
                chat, task_action="destroy", namespace=str(chat))
            assert "未知 task_action" in bad_action

            handle = await execute_bash(
                chat, command="echo routed-ok", run_in_background=True,
                description="路由验证", namespace=str(chat))
            assert handle.startswith("✅ 后台任务已启动")
        finally:
            await _shutdown_all_and_reset()

    _run(scenario())


@requires_posix
def test_dispatch_tool_call_forwards_background_params(tmp_path: Any, monkeypatch: Any) -> None:
    """dispatch 端到端：新参数从 dispatch_tool_call 一路转发到 bash_background。"""
    _isolate(tmp_path, monkeypatch)
    _reset_registries()
    from tool_dispatch import dispatch_tool_call

    async def scenario() -> None:
        try:
            chat = 990108
            listing = await dispatch_tool_call(
                "bash", {"description": "列表", "task_action": "list"}, chat_id=chat)
            assert "后台任务列表" in listing

            handle = await dispatch_tool_call(
                "bash",
                {"description": "后台回声", "command": "echo dispatch-bg-ok",
                 "run_in_background": True},
                chat_id=chat,
            )
            assert handle.startswith("✅ 后台任务已启动")
            task_id = _TASK_ID_RE.search(handle).group(0)
            task = bash_background._TASKS[(chat, workspace_paths.workspace_namespace(chat))][task_id]
            await _wait_terminal(task)
            assert task.status == "done"
        finally:
            await _shutdown_all_and_reset()

    _run(scenario())
