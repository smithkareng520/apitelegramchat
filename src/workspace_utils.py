# workspace_utils.py
import asyncio
import logging
from pathlib import Path
from workspace_paths import (
    agent_home, workspace_root, workspace_namespace,
    workspace_upload_root, workspace_download_root,
)

logger = logging.getLogger(__name__)

# 工作区是本地运行时文件系统。R2 持久化由具体业务模块拥有，
# 普通 workspace 文件没有通用的 R2 同步入口。


class _LockRegistry:
    """按需创建、按 key 复用的 asyncio.Lock 注册表。

    把此前暴露为模块级全局变量的两组 "dict + 注册表锁 + get-or-create"
    （_workspace_locks/_workspace_locks_lock、_workspace_init_locks/
    _workspace_init_locks_lock）收拢进类：可变 dict 不再可被任意导入方
    绕过锁直接改写，get_or_create 在注册表锁内原子完成。

    并发模型说明（为什么不用 contextvars）：这里的锁按 workspace key
    全局共享——同一个 workspace 的所有并发协程必须互斥，属于「跨任务
    共享」状态；contextvars 是「每任务/每请求隔离」，语义正好相反。
    锁实例的使用（async with）发生在注册表锁之外，不存在嵌套持锁，
    因此不会死锁。锁数量与 workspace 数量同阶（每 namespace 一把），
    有界且生命周期与进程相同，无需淘汰。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def get_or_create(self, key: str) -> asyncio.Lock:
        """原子地取 key 对应的锁，不存在则创建。"""
        async with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock


# workspace 访问锁：保护同一聊天的本地文件操作，避免并发修改。
_workspace_file_locks = _LockRegistry()

_workspace_initialized: set[str] = set()
# workspace 初始化专用锁；与文件操作锁分离，避免嵌套死锁。
_workspace_init_lock_registry = _LockRegistry()


async def _get_workspace_lock(chat_id: int, namespace: str | None = None) -> asyncio.Lock:
    """获取或创建该用户/作用域的 workspace 锁。"""
    return await _workspace_file_locks.get_or_create(workspace_namespace(chat_id, namespace))


async def _get_workspace_init_lock(key: str) -> asyncio.Lock:
    """获取 workspace 初始化专用锁；与文件操作锁分离，避免嵌套死锁。"""
    return await _workspace_init_lock_registry.get_or_create(key)


async def _ensure_runtime_workspace(chat_id: int, namespace: str | None = None) -> None:
    """Ensure the runtime workspace tree (agent home + upload/ + download/) exists.

    This function is intentionally safe to call before every tool invocation.
    It MUST NOT synchronize packaged skills: ``workspace/skills`` is runtime
    state and may contain files created or edited by the agent/user.

    upload/ and download/ are pre-created here (rather than lazily on first
    use) because bash starts with cwd=agent home and the model almost
    immediately tries `cp out.txt upload/out.txt` or `cat download/x.pdf`.
    Without pre-creating these subtrees, the very first such command fails
    with "No such file or directory" — forcing the model to spend an extra
    `mkdir -p upload/` round before doing the real work. This is the
    initialization boundary, not a per-tool concern.

    v2.3.1：upload/ 与 download/ 挂在 agent 家目录（即 workspace 根本身）
    下；首次访问家目录时会自动把遗留布局条目迁移到位（见
    workspace_paths.agent_home）。
    """
    workspace = workspace_root(chat_id, namespace)
    workspace.mkdir(parents=True, exist_ok=True)
    # 显式预创建 upload/ 与 download/：两者都位于 agent 家目录下，
    # workspace_upload_root / workspace_download_root 是幂等的（mkdir
    # exist_ok + chmod 0o700），重复调用不会出错；首次调用就把这两棵
    # 子树准备好（并顺带触发家目录的一次性迁移）。
    workspace_upload_root(chat_id, namespace)
    workspace_download_root(chat_id, namespace)


async def _ensure_workspace_initialized(chat_id: int, namespace: str | None = None) -> None:
    """Initialize packaged/runtime skills once for this workspace."""
    resolved_namespace = workspace_namespace(chat_id, namespace)
    lock = await _get_workspace_init_lock(resolved_namespace)
    async with lock:
        home = agent_home(chat_id, resolved_namespace)
        marker = home / ".skills_initialized"
        if resolved_namespace in _workspace_initialized or marker.is_file():
            _workspace_initialized.add(resolved_namespace)
            return
        try:
            from skills_r2 import initialize_workspace_skills
            await initialize_workspace_skills(home, resolved_namespace)
            marker.write_text("initialized\n", encoding="utf-8")
            _workspace_initialized.add(resolved_namespace)
        except Exception as exc:
            logger.warning(
                "初始化 skills 到 workspace 失败 namespace=%s: %s",
                resolved_namespace, exc,
            )


# ========== 可选：初始化工作区（后台执行） ==========

async def init_workspace(chat_id: int, namespace: str | None = None) -> None:
    """Initialize the workspace and packaged skills once for this workspace."""
    try:
        await _ensure_workspace_initialized(chat_id, namespace)
    except Exception as e:
        logger.error(f"Workspace 初始化失败: {e}")


# 后台 init_workspace 任务强引用集：事件循环只持有任务的弱引用，若调用方
# 不保存返回值，任务可能在执行中途被 GC 回收（CPython asyncio 官方文档
# 明确警告的坑），表现为 workspace 预初始化静默消失、首个工具调用退化为
# 同步 no-op 初始化。此集合保证任务存活到自然结束。
_workspace_init_tasks: set = set()


def schedule_workspace_init(chat_id: int, namespace: str | None = None) -> asyncio.Task:
    """后台调度 init_workspace 并保留强引用（fire-and-forget 的安全封装）。

    所有"预初始化工作区"的调用点都应使用本函数而非裸
    ``asyncio.create_task(init_workspace(chat_id))``。
    """
    task = asyncio.create_task(init_workspace(chat_id, namespace))
    _workspace_init_tasks.add(task)
    task.add_done_callback(_workspace_init_tasks.discard)
    return task
