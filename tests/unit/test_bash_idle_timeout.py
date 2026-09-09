# =====================================================================
# tests/unit/test_bash_idle_timeout.py — bash 双层超时（v2.4 防卡死）
# =====================================================================
# 覆盖错误日志反馈的问题：「bash 遇到网络不可达等静默挂起请求时会卡满
# 整个总超时（300s）」。修复后 bash 读循环采用双层超时：
#   - idle：持续无输出超过 SANDBOX_IDLE_TIMEOUT_SEC → 立即 kill（核心）；
#   - total：总预算上限（原行为）。
# 同时覆盖 sandbox.build_sandbox_env 的 sitecustomize 注入（沙箱内 Python
# 进程默认 socket 超时）与常见 CLI 工具超时环境变量。
#
# 集成用例真实 spawn bash 进程：本机/CI 内核可能 < 5.13（Landlock 不可用），
# 统一 monkeypatch sandbox._apply_landlock 直通；生产路径不受影响
# （preexec_fn 运行于 fork 后的子进程，继承父进程已 patch 的模块状态）。
# =====================================================================

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import sandbox
import workspace_paths
from bash_session import (
    BashSession,
    _format_idle_timeout_message,
    _normalize_requested_timeout,
)
from sandbox import (
    SANDBOX_IDLE_TIMEOUT_SEC,
    SANDBOX_TIMEOUT_HARD_MAX,
    SANDBOX_TIMEOUT_SEC,
)


# ---------------------------------------------------------------------------
# 纯逻辑：timeout 参数规范化
# ---------------------------------------------------------------------------
def test_normalize_timeout_defaults() -> None:
    """不传 timeout → 默认总超时 + 空闲保护同时生效。"""
    assert _normalize_requested_timeout(None) == (
        SANDBOX_TIMEOUT_SEC,
        SANDBOX_IDLE_TIMEOUT_SEC,
    )


def test_normalize_timeout_explicit_disables_idle() -> None:
    """显式传 timeout → (clamped, None)：禁用空闲保护。"""
    total, idle = _normalize_requested_timeout(120)
    assert total == 120
    assert idle is None


def test_normalize_timeout_clamps_to_hard_max() -> None:
    """超出硬上限 → clamp 到 SANDBOX_TIMEOUT_HARD_MAX。"""
    total, idle = _normalize_requested_timeout(999_999)
    assert total == SANDBOX_TIMEOUT_HARD_MAX
    assert idle is None


def test_normalize_timeout_clamps_to_minimum() -> None:
    """过小值 → clamp 到下限 5s。"""
    total, idle = _normalize_requested_timeout(1)
    assert total == 5
    assert idle is None


def test_normalize_timeout_invalid_falls_back() -> None:
    """非法值（字符串 / bool）→ 静默回退默认双层配置。"""
    default = (SANDBOX_TIMEOUT_SEC, SANDBOX_IDLE_TIMEOUT_SEC)
    assert _normalize_requested_timeout("abc") == default  # type: ignore[arg-type]
    assert _normalize_requested_timeout(True) == default


def test_idle_timeout_message_is_actionable() -> None:
    """空闲超时消息必须说清原因并给出可操作的自纠路径。"""
    msg = _format_idle_timeout_message(60.0)
    assert "no output for 60s" in msg
    assert "unreachable network call" in msg
    # 指向具体修复手段
    assert "--connect-timeout" in msg
    assert "timeout" in msg  # bash timeout 参数逃生通道


# ---------------------------------------------------------------------------
# sitecustomize 注入：沙箱内 Python 默认 socket 超时
# ---------------------------------------------------------------------------
@pytest.fixture()
def sandbox_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict]:
    """隔离 data_root 后构建一次沙箱环境变量。"""
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(sandbox, "_apply_landlock", lambda path: True)
    workspace_paths.data_root.cache_clear()
    workspace_paths.workspaces_root.cache_clear()
    try:
        env = sandbox.build_sandbox_env(tmp_path / "ws", 42, "ns42")
        yield env
    finally:
        workspace_paths.data_root.cache_clear()
        workspace_paths.workspaces_root.cache_clear()


def test_build_env_injects_sitecustomize_and_cli_timeouts(
    sandbox_env: dict,
) -> None:
    env = sandbox_env
    # PYTHONPATH 指向 runtime bin（sitecustomize 所在目录）
    pythonpath = env.get("PYTHONPATH", "")
    assert pythonpath and Path(pythonpath).name == "bin"
    # sitecustomize.py 已写入且包含注入标记与 setdefaulttimeout 调用
    sc = Path(pythonpath) / "sitecustomize.py"
    assert sc.is_file()
    content = sc.read_text(encoding="utf-8")
    assert "sandbox-sc-v1" in content
    assert "setdefaulttimeout" in content
    # 子进程可读取的超时配置与 CLI 工具超时环境变量
    assert env.get("SANDBOX_SOCKET_TIMEOUT_SEC") == "15"
    assert env.get("PIP_DEFAULT_TIMEOUT") == "15"
    assert env.get("GIT_HTTP_LOW_SPEED_LIMIT") == "1000"
    assert env.get("GIT_HTTP_LOW_SPEED_TIME") == "30"
    assert env.get("npm_config_fetch_timeout") == "60000"


def test_sitecustomize_sets_real_socket_default(sandbox_env: dict) -> None:
    """端到端：在注入的环境里启动真实 Python，默认 socket 超时应生效。"""
    env = dict(sandbox_env)
    # PYTHONPATH 与 SANDBOX_SOCKET_TIMEOUT_SEC 已在 env 中；补充安全兜底。
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    proc = subprocess.run(
        [sys.executable, "-c", "import socket; print(socket.getdefaulttimeout())"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "15.0"


# ---------------------------------------------------------------------------
# 集成：真实 bash 会话的双层超时行为（Landlock 直通，见文件头说明）
# ---------------------------------------------------------------------------

def test_dumpable_prctl_einval_is_reported_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restricted container kernels may reject PR_SET_DUMPABLE with EINVAL.

    The failure is a host capability/security-policy issue, not a reason to
    spam one ERROR for every bash child. The result is cached and the warning
    remains explicit so operators can fix the host policy.
    """
    class FakeLibc:
        def prctl(self, option: int, value: int, *args: int) -> int:
            assert option == sandbox.PR_SET_DUMPABLE
            assert value == 0
            return -1

    monkeypatch.setattr(sandbox, "_libc", FakeLibc())
    monkeypatch.setattr(sandbox.ctypes, "get_errno", lambda: 22)
    sandbox._dumpable_state = None

    warnings: list[str] = []
    monkeypatch.setattr(sandbox.logger, "warning", lambda message, *args: warnings.append(str(message)))

    first = sandbox._set_undumpable()
    second = sandbox._set_undumpable()

    assert first is False
    assert second is False
    assert sandbox._dumpable_state is False
    assert len(warnings) == 1
    assert "EINVAL" in warnings[0]

def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _new_session(tmp_path: Any, monkeypatch: Any) -> BashSession:
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(sandbox, "_apply_landlock", lambda path: True)
    workspace_paths.data_root.cache_clear()
    workspace_paths.workspaces_root.cache_clear()
    return BashSession(424242)


def test_idle_timeout_kills_silent_command(tmp_path: Any, monkeypatch: Any) -> None:
    """无输出静默命令（模拟网络挂起）应在 idle 阈值附近被杀，而非等满总超时。"""
    async def scenario() -> None:
        session = _new_session(tmp_path, monkeypatch)
        try:
            t0 = time.monotonic()
            out = await session.execute(
                "sleep 999", total_timeout=300, idle_timeout=2
            )
            elapsed = time.monotonic() - t0
            assert "no output for" in out
            assert "idle limit" in out
            # 远小于 300s 总超时，且确实等过了 idle 阈值
            assert elapsed < 60
            assert elapsed >= 1.5
            # 会话已被清理关闭（下次 execute 会重新 spawn）
            assert session.proc is None or session.proc.returncode is not None
        finally:
            await session.close()

    _run(scenario())


def test_idle_timeout_preserves_partial_output(tmp_path: Any, monkeypatch: Any) -> None:
    """idle 超时前已产生的输出应随错误一并回传给模型。"""
    async def scenario() -> None:
        session = _new_session(tmp_path, monkeypatch)
        try:
            out = await session.execute(
                "echo alive-marker; sleep 999", total_timeout=300, idle_timeout=2
            )
            assert "no output for" in out
            assert "Captured partial output" in out
            assert "alive-marker" in out
        finally:
            await session.close()

    _run(scenario())


def test_total_timeout_without_idle_guard(tmp_path: Any, monkeypatch: Any) -> None:
    """禁用 idle 保护（显式 timeout 语义）时仅总超时生效。"""
    async def scenario() -> None:
        session = _new_session(tmp_path, monkeypatch)
        try:
            t0 = time.monotonic()
            out = await session.execute(
                "sleep 999", total_timeout=2, idle_timeout=None
            )
            elapsed = time.monotonic() - t0
            assert "Command timed out after 2 seconds" in out
            assert elapsed < 30
            assert elapsed >= 1.5
        finally:
            await session.close()

    _run(scenario())


def test_chatty_command_survives_idle_guard(tmp_path: Any, monkeypatch: Any) -> None:
    """持续输出的长命令不受 idle 保护影响（每 0.5s 一次输出 < 2s 阈值）。"""
    async def scenario() -> None:
        session = _new_session(tmp_path, monkeypatch)
        try:
            out = await session.execute(
                "for i in 1 2 3 4 5; do echo tick-$i; sleep 0.5; done",
                total_timeout=300,
                idle_timeout=2,
            )
            assert "Exit code: 0" in out
            for i in range(1, 6):
                assert f"tick-{i}" in out
            assert "no output for" not in out
        finally:
            await session.close()

    _run(scenario())


def test_idle_timeout_in_isolated_heredoc_path(tmp_path: Any, monkeypatch: Any) -> None:
    """heredoc（隔离执行路径）同样受 idle 保护，并保留部分输出。"""
    async def scenario() -> None:
        session = _new_session(tmp_path, monkeypatch)
        try:
            cmd = "cat <<'EOF'\nheredoc-marker\nEOF\nsleep 999"
            out = await session.execute(cmd, total_timeout=300, idle_timeout=2)
            assert "no output for" in out
            assert "heredoc-marker" in out
        finally:
            await session.close()

    _run(scenario())
