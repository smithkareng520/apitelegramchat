# =====================================================================
# tests/unit/test_ingest_supervisor.py
# =====================================================================
# 回归护栏：2026-09-15「进程活着但永久失聪」事故。
#
# 现场特征：/health 恒 200、loop_lag=0.00s、心跳每分钟准点，但
# queue=0 / active_tasks=0 / tasks=12 连续数十分钟一动不动，日志里
# 没有任何一行 polling fetched / queued / worker processing——摄取
# 通道已死，而系统里没有任何人知道。
#
# 本文件锁死三条不变量：
#   1. 非关停来源的 CancelledError 不得杀死轮询循环；
#   2. 轮询子任务无论以何种方式结束，主管都必须把它拉起来；
#   3. 子任务活着但长时间拉不到东西（停摆）时，主管必须强制重启；
# 外加：摄取通道断了 is_ingest_broken() 必须为真（/health 据此返回 503）。
# =====================================================================
import asyncio

import pytest

import telegram_polling as tp


@pytest.fixture(autouse=True)
def _reset_module_state():
    """每个用例前后复位模块级状态，避免互相污染。"""
    tp._started = False
    tp._shutting_down = False
    tp._stop_requested = False
    tp._last_success_monotonic = None
    tp._poll_task = None
    tp._restart_count = 0
    yield
    tp._shutting_down = True
    tp._stop_requested = True


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------
# 1. 误取消不得杀死轮询循环
# ---------------------------------------------------------------------
def test_stray_cancellation_does_not_kill_poll_loop(monkeypatch):
    """aiohttp 超时取消逸出这类误取消，循环必须吸收后继续。

    旧版此处无条件 raise —— 一次就让 bot 永久收不到消息。
    """
    monkeypatch.setattr(tp, "_BACKOFF_MIN", 0.01)
    monkeypatch.setattr(tp, "_BACKOFF_MAX", 0.01)
    calls = {"n": 0}

    async def fake_fetch(offset):
        # 真实实现总要 await 网络 IO；这里显式让出一次，避免假实现把
        # 事件循环占死（测试自身的坑，与被测逻辑无关）。
        await asyncio.sleep(0)
        calls["n"] += 1
        if calls["n"] == 1:
            # 模拟 aiohttp ClientTimeout 的取消信号从请求里逸出
            raise asyncio.CancelledError()
        if calls["n"] == 4:
            return [{"update_id": 100, "message": {"text": "hi"}}]
        return []  # 其余轮次都是正常空轮询

    monkeypatch.setattr(tp, "_fetch_updates", fake_fetch)

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(tp.poll_updates_forever(queue))
        update = await asyncio.wait_for(queue.get(), timeout=5)
        tp._stop_requested = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return update

    update = _run(scenario())
    # 循环在误取消之后活了下来，并且照常把 update 投进队列
    assert update["update_id"] == 100
    assert calls["n"] >= 4


# ---------------------------------------------------------------------
# 2. 子任务死掉，主管必须拉起来
# ---------------------------------------------------------------------
def test_supervisor_restarts_dead_poll_loop(monkeypatch):
    """轮询子任务异常退出后，主管按退避重启，通道不会永久失聪。"""
    monkeypatch.setattr(tp, "_BACKOFF_MIN", 0.01)
    monkeypatch.setattr(tp, "_BACKOFF_MAX", 0.01)
    monkeypatch.setattr(tp, "_SUPERVISOR_INTERVAL", 0.01)
    starts = {"n": 0}

    async def exploding_loop(queue):
        starts["n"] += 1
        if starts["n"] < 3:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)

    monkeypatch.setattr(tp, "poll_updates_forever", exploding_loop)

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        sup = asyncio.create_task(tp._supervise(queue))
        for _ in range(200):
            if starts["n"] >= 3:
                break
            await asyncio.sleep(0.02)
        tp._shutting_down = True
        sup.cancel()
        try:
            await sup
        except asyncio.CancelledError:
            pass

    _run(scenario())
    assert starts["n"] >= 3, "主管没有重启已死的轮询子任务"
    assert tp._restart_count >= 2


# ---------------------------------------------------------------------
# 3. 停摆（任务活着但拉不到东西）也要重启
# ---------------------------------------------------------------------
def test_supervisor_restarts_stalled_poll_loop(monkeypatch):
    """子任务还在跑但长时间没有一次成功的 getUpdates → 强制重启。

    覆盖"请求挂死/连接池饿死"这类不会抛异常的静默故障。
    """
    monkeypatch.setattr(tp, "_BACKOFF_MIN", 0.01)
    monkeypatch.setattr(tp, "_BACKOFF_MAX", 0.01)
    monkeypatch.setattr(tp, "_SUPERVISOR_INTERVAL", 0.01)
    monkeypatch.setattr(tp, "STALL_SECONDS", 0.05)
    starts = {"n": 0}

    async def hung_loop(queue):
        starts["n"] += 1
        await asyncio.sleep(3600)  # 永远拉不到，也永远不报错

    monkeypatch.setattr(tp, "poll_updates_forever", hung_loop)

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        sup = asyncio.create_task(tp._supervise(queue))
        for _ in range(200):
            if starts["n"] >= 2:
                break
            await asyncio.sleep(0.02)
        tp._shutting_down = True
        sup.cancel()
        try:
            await sup
        except asyncio.CancelledError:
            pass

    _run(scenario())
    assert starts["n"] >= 2, "停摆的轮询子任务没有被主管强制重启"


# ---------------------------------------------------------------------
# 4. 摄取通道断了，健康判定必须变红
# ---------------------------------------------------------------------
def test_is_ingest_broken_only_after_start():
    """没启用轮询（webhook 模式 / 单测进程）时不得误报不健康。"""
    assert tp.is_ingest_broken() is False


def test_is_ingest_broken_when_task_dead():
    """启动过轮询但任务已结束 → /health 必须能看出来。"""

    async def scenario():
        async def noop():
            return None

        task = asyncio.create_task(noop())
        await task
        tp._started = True
        tp._poll_task = task
        return tp.is_ingest_broken()

    assert _run(scenario()) is True


def test_is_ingest_broken_false_during_shutdown():
    """关停窗口内不应把正常收尾误判为故障（避免部署期无谓的 503）。"""

    async def scenario():
        async def noop():
            return None

        task = asyncio.create_task(noop())
        await task
        tp._started = True
        tp._poll_task = task
        tp._shutting_down = True
        return tp.is_ingest_broken()

    assert _run(scenario()) is False
