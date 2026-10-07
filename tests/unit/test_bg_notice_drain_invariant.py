# =====================================================================
# tests/unit/test_bg_notice_drain_invariant.py — drain 收敛不变式
# =====================================================================
# 用户诉求（本轮重构的验收标准）：
#   "drain 接线太散，4 个手工注入点 + 多条『不消费』路径，未来任何新增
#    模型调用路径忘了 drain 就静默丢通知，且最难测。建议把 drain+append
#    收敛到最低公共调用点（包住 agentic 循环内的 post 和 _call_api），
#    最多 2 个 wrapper；再加一条不变式测试：『凡到达模型调用的路径，
#    队列必被 drain』。"
#
# 落地结构（src 中仅存在两个 drain 注入点）：
#   ① ai_handlers._call_api 函数入口自守卫——全部 chat 协议模型调用
#      （USER / TIMER / 静默 / 未来新增路由）的唯一最低公共入口；
#   ② ai.agentic_loops._media_loop_with_notices——image/video 生成
#      （agentic 循环内的 POST）两条循环的唯一入口，get_ai_response
#      与媒体向导提交路径共用。
#
# 本文件两层守护：
#   - 结构层（AST）：模型调用函数只能经 wrapper 触达 / 入口必含 drain，
#     新增路径想绕开接线会在本测试直接编译期失败；
#   - 行为层（运行时）：两个注入点真实 drain（通知作为尾部 system 消息
#     搭车、队列清空、drain 先于模型请求发生）、push/drain 跨线程守恒
#     （不丢不重）、重启 was-running 孤儿补推「因重启被中止」。
#
# 说明：subagent 的迷你 agentic 循环（subagent_tool）刻意不 drain——
# 子 agent 持全新上下文，父 chat 的待送通知必须留给父请求搭车；且子
# 循环只会运行在已被 _call_api drain 过的父回合内部，请求级不变式
# 依然成立。
# =====================================================================
import ast
import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

import bash_background
import sandbox
import workspace_paths
from ai.agentic_loops import _media_loop_with_notices
from ai_handlers import _call_api
from config import SUPPORTED_MODELS
from core.messages import Message

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PROJECT_ROOT / "src"

_MODEL_LOOP_FUNCS = ("_agentic_loop_native_image", "_agentic_loop_native_video")


# ---------------------------------------------------------------------------
# AST 工具
# ---------------------------------------------------------------------------
def _src_files() -> Iterator[Path]:
    for p in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def _call_name(node: ast.Call) -> Optional[str]:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _calls_in(body: list[ast.stmt]) -> Iterator[ast.Call]:
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call):
            yield node


def _find_func(tree: ast.Module, name: str) -> Optional[ast.FunctionDef]:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


# ---------------------------------------------------------------------------
# 结构不变式：凡到达模型调用的路径，队列必被 drain
# ---------------------------------------------------------------------------
def test_invariant_call_api_drains_at_entry_before_model_call() -> None:
    """注入点①：_call_api 函数体首部必须调用 _append_bg_task_notices，
    且先于 adapter.run_agent_loop（真正的模型请求）——通知必须在本轮
    请求构建完成前进入 messages。"""
    tree = ast.parse((_SRC / "ai_handlers.py").read_text(encoding="utf-8"))
    fn = _find_func(tree, "_call_api")
    assert fn is not None, "ai_handlers._call_api 不见了？"
    drain_lines = [
        c.lineno for c in _calls_in(fn.body)
        if _call_name(c) == "_append_bg_task_notices"]
    model_lines = [
        c.lineno for c in _calls_in(fn.body)
        if _call_name(c) == "run_agent_loop"]
    assert drain_lines, "_call_api 入口没有 drain——不变式被破坏"
    assert model_lines, "_call_api 不再调用 run_agent_loop？路由结构已变，请同步更新本测试"
    assert min(drain_lines) < min(model_lines), (
        "drain 必须发生在模型请求之前（通知要随本轮请求搭车，而不是"
        "请求已发出才消费——那样通知会被无声吞掉）")
    # drain 的前两个实参必须是本函数的 messages / chat_id（钉死接线，
    # 未来改签名传错参数会在此直接失败）
    drain_call = next(c for c in _calls_in(fn.body)
                      if _call_name(c) == "_append_bg_task_notices")
    args = drain_call.args
    assert len(args) >= 2, "drain 调用缺少 messages/chat_id 实参"
    assert isinstance(args[0], ast.Name) and args[0].id == "messages"
    assert isinstance(args[1], ast.Name) and args[1].id == "chat_id"


def test_invariant_media_loops_only_reachable_via_wrapper() -> None:
    """注入点②：_agentic_loop_native_image / _agentic_loop_native_video
    在 src/ 全仓不允许出现直接调用——只能作为 _media_loop_with_notices
    的首参传入。新增媒体调用路径因此不可能绕过 drain。"""
    direct_calls: list[str] = []
    wrapper_calls: list[ast.Call] = []
    for path in _src_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in _MODEL_LOOP_FUNCS:
                direct_calls.append(f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno}")
            elif name == "_media_loop_with_notices":
                wrapper_calls.append(node)
    assert not direct_calls, (
        f"媒体生成循环被直接调用（必须经 _media_loop_with_notices 包住）: {direct_calls}")
    assert wrapper_calls, "没有任何 _media_loop_with_notices 调用点？"
    # 每个 wrapper 调用的首参必须是两条媒体循环之一（名字引用，非调用）
    for call in wrapper_calls:
        first = call.args[0] if call.args else None
        assert isinstance(first, ast.Name) and first.id in _MODEL_LOOP_FUNCS, (
            f"_media_loop_with_notices 的首参必须是媒体循环函数引用: "
            f"{ast.dump(call)[:200]}")


def test_invariant_media_wrapper_contains_drain_and_single_impl() -> None:
    """注入点②内部必须 drain；且 _append_bg_task_notices 全仓只有一个
    实现（防止 wiring 再次散开成多处拷贝）。"""
    tree = ast.parse((_SRC / "ai" / "agentic_loops.py").read_text(encoding="utf-8"))
    wrapper = _find_func(tree, "_media_loop_with_notices")
    assert wrapper is not None, "_media_loop_with_notices 不见了？"
    assert any(_call_name(c) == "_append_bg_task_notices" for c in _calls_in(wrapper.body)), (
        "_media_loop_with_notices 内部没有 drain——不变式被破坏")

    impls: list[str] = []
    for path in _src_files():
        t = ast.parse(path.read_text(encoding="utf-8"))
        for node in t.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "_append_bg_task_notices":
                impls.append(str(path.relative_to(_PROJECT_ROOT)))
    assert impls == ["src/ai/agentic_loops.py"], (
        f"_append_bg_task_notices 必须只在 agentic_loops.py 实现一处: {impls}")


# ---------------------------------------------------------------------------
# 行为验证：注入点① _call_api 真实 drain
# ---------------------------------------------------------------------------
def _isolate(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("APITELEGRAMCHAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APITELEGRAMCHAT_WORKSPACES_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(sandbox, "_apply_landlock", lambda path: True)
    workspace_paths.data_root.cache_clear()
    workspace_paths.workspaces_root.cache_clear()


def _reset_notices() -> None:
    bash_background._TASKS.clear()
    bash_background._LOADED_KEYS.clear()
    bash_background._PENDING_NOTICES.clear()


@pytest.mark.asyncio
async def test_call_api_drains_notices_as_tail_system_message(
        tmp_path: Any, monkeypatch: Any) -> None:
    """经 _call_api 到达模型的所有路径：待送通知被 drain 成一条尾部
    system 消息，且发生在模型请求发出之前；队列随之清空。"""
    _isolate(tmp_path, monkeypatch)
    _reset_notices()
    import ai_handlers

    class _FakeAdapter:
        def __init__(self) -> None:
            self.captured: list = []

        async def run_agent_loop(self, **kwargs: Any) -> tuple:
            self.captured = list(kwargs.get("messages") or [])
            return "fake-final", None, []

    fake = _FakeAdapter()
    monkeypatch.setattr(ai_handlers, "resolve_chat_adapter", lambda _mi: fake)

    chat, ns = 991001, "991001"
    bash_background.push_completion_notice(chat, ns, "notice-via-call-api")
    messages = [Message.system("base-prefix")]

    model_info = SUPPORTED_MODELS["agnes-3.0-flash"]
    raw, _, _ = await _call_api(
        "agnes-3.0-flash", model_info, messages, chat, None,
        tools=[], journal=None, workspace_namespace=ns,
    )
    assert raw == "fake-final"
    # 模型请求看到的通知：追加在稳定前缀之后（尾部搭车）
    assert len(fake.captured) == 2
    assert fake.captured[0].role == "system" and "base-prefix" in fake.captured[0].text()
    tail = fake.captured[-1]
    assert tail.role == "system" and "notice-via-call-api" in tail.text()
    # 队列已清空（消费即取走）
    assert bash_background.drain_completion_notices(chat, ns) == []
    _reset_notices()


@pytest.mark.asyncio
async def test_media_loop_wrapper_drains_and_strips_namespace(
        tmp_path: Any, monkeypatch: Any) -> None:
    """经 _media_loop_with_notices 到达生成端点的路径同样 drain；
    namespace 仅用于队列键解析，不透传给循环函数。"""
    _isolate(tmp_path, monkeypatch)
    _reset_notices()

    chat, ns = 991002, "991002"
    bash_background.push_completion_notice(chat, ns, "notice-via-media-loop")
    messages: list = [Message.system("base")]

    seen: dict = {}

    async def fake_loop(**kwargs: Any) -> tuple:
        seen.update(kwargs)
        seen["_messages_snapshot"] = list(kwargs["messages"])
        return "VIDEO_SENT", None, []

    raw, usage, new_msgs = await _media_loop_with_notices(
        fake_loop,
        current_model="some-video-model", messages=messages,
        builder=None, chat_id=chat, journal=None, namespace=ns,
    )
    assert raw == "VIDEO_SENT" and usage is None and new_msgs == []
    # 循环收到的 messages 已带尾部通知
    assert len(seen["_messages_snapshot"]) == 2
    assert "notice-via-media-loop" in seen["_messages_snapshot"][-1].text()
    # namespace 被摘除（媒体循环签名不接受该参数）
    assert "namespace" not in seen
    assert bash_background.drain_completion_notices(chat, ns) == []
    _reset_notices()


# ---------------------------------------------------------------------------
# 行为验证：push/drain 跨线程守恒（每 chat 一把锁 + 整段换空）
# ---------------------------------------------------------------------------
def test_push_drain_thread_conservation_no_loss_no_dup(
        tmp_path: Any, monkeypatch: Any) -> None:
    """多线程并发 push + drain：通知总数守恒——既不丢失（push 追加到
    被 drain 孤儿化的列表上）也不双发（同一段队列被消费两次）。
    上限截断会主动丢最旧，测试期间临时放大上限以观测纯并发行为。"""
    _isolate(tmp_path, monkeypatch)
    _reset_notices()
    monkeypatch.setattr(bash_background, "_PENDING_NOTICES_MAX_PER_CHAT", 1_000_000)

    chat, ns = 991003, "991003"
    # 预热懒加载（建目录），让工作线程只做纯队列操作
    assert bash_background.drain_completion_notices(chat, ns) == []

    total, n_threads = 800, 8
    per_thread = total // n_threads
    collected: list[str] = []
    collect_lock = threading.Lock()
    pushers_done = threading.Event()

    def pusher(tid: int) -> None:
        for i in range(per_thread):
            bash_background.push_completion_notice(chat, ns, f"n-{tid}-{i}")

    def drainer() -> None:
        while True:
            got = bash_background.drain_completion_notices(chat, ns)
            if got:
                with collect_lock:
                    collected.extend(got)
            elif pushers_done.is_set():
                # pushers 全部结束后再 drain 一次仍为空 → 收尾
                got = bash_background.drain_completion_notices(chat, ns)
                if not got:
                    return
                with collect_lock:
                    collected.extend(got)

    threads = [threading.Thread(target=pusher, args=(t,)) for t in range(n_threads)]
    drainers = [threading.Thread(target=drainer) for _ in range(3)]
    for t in drainers:
        t.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pushers_done.set()
    for t in drainers:
        t.join()

    assert len(collected) == total, (
        f"通知丢失或双发：收集 {len(collected)} / 期望 {total}")
    assert len(set(collected)) == total, "同一通知被消费了两次（双发）"
    _reset_notices()


# ---------------------------------------------------------------------------
# 行为验证：重启恢复三分（completed / was-running 活 / was-running 死）
# ---------------------------------------------------------------------------
def _spawn_dead_pid() -> int:
    proc = subprocess.Popen(
        ["bash", "-c", "exit 0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait(timeout=10)
    # proc.pid 已退出；内核短期内不会复用
    return proc.pid


def test_restart_was_running_dead_orphan_backfills_aborted_notice(
        tmp_path: Any, monkeypatch: Any) -> None:
    """.json 无 .exit 且进程已死（关机时还在跑、进程组已死）：
    状态 lost 并补推「因重启被中止」；有 .exit 的 completed 条目只读
    恢复、不重发通知。"""
    if sys.platform == "win32":
        pytest.skip("依赖 /proc 与 POSIX pid 语义")
    _isolate(tmp_path, monkeypatch)
    _reset_notices()

    chat, ns = 991004, "991004"
    tdir = bash_background._tasks_dir(chat, ns)
    tdir.mkdir(parents=True, exist_ok=True)
    dead_pid = _spawn_dead_pid()

    def _write_state(task_id: str, status: str, pid: int) -> None:
        payload = {
            "task_id": task_id, "chat_id": chat, "namespace": ns,
            "command": "sleep 999", "description": "重启恢复测试",
            "cwd": "", "pid": pid,
            "log_path": str(tdir / f"{task_id}.log"),
            "started_at": 1_000_000.0, "lifetime": 3600,
            "status": status,
        }
        (tdir / f"{task_id}.json").write_text(
            __import__("json").dumps(payload), encoding="utf-8")

    # A：completed（.exit 已落）→ 只读恢复
    _write_state("bg-aaaaaaaa", "done", dead_pid)
    (tdir / "bg-aaaaaaaa.exit").write_text("done", encoding="utf-8")
    # B：was-running 且进程已死（关机被杀 / 关机窗口内退出）
    _write_state("bg-bbbbbbbb", "running", dead_pid)

    bash_background._ensure_registry_loaded(chat, ns)
    notices = bash_background.drain_completion_notices(chat, ns)

    # 只有 was-running 死孤儿补推通知
    assert len(notices) == 1, f"应恰好补推一条通知: {notices}"
    assert "bg-bbbbbbbb" in notices[0]
    assert "因重启被中止" in notices[0]
    assert "退出码未知" in notices[0]

    tasks = bash_background._TASKS[(chat, ns)]
    assert tasks["bg-aaaaaaaa"].status == "done"  # completed 只读恢复
    assert tasks["bg-bbbbbbbb"].status == "lost"
    assert bash_background.drain_completion_notices(chat, ns) == []
    _reset_notices()
