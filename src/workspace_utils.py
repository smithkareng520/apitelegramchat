# workspace_utils.py
import asyncio
import hashlib
import json
import os
import logging
from pathlib import Path
from workspace_paths import (
    agent_home, workspace_root, workspace_namespace,
    workspace_skills_root,
    workspace_upload_root, workspace_download_root,
)

from s3_utils import (
    upload_bytes_to_r2,
    download_from_r2,
    delete_r2_object,
    list_r2_objects,
    is_r2_configured,
)

logger = logging.getLogger(__name__)

# R2 持久化只发生在明确选择的用户文件上；运行时树永不进行全量同步。


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


# ========== 用户 skills/ 目录的 R2 持久化（恢复 + 备份） ==========
# workspace/skills 里的运行时技能（用户让 agent 创建/修改的技能）此前是
# 纯本地状态：数据目录易失的部署（未挂持久盘的容器、本机 /tmp）在服务
# 重启后整个 workspace 被清空，自建技能永久丢失，只有打包技能会被重新
# 拷回。这里仿照 todo/memory 的定向同步模式，把该目录按 `skills/{ns}/`
# 前缀接入 R2：
#   - 恢复（workspace 首次初始化时）：R2 备份里存在而本地缺失的文件拉
#     回来，绝不覆盖本地已有文件（workspace 运行时内容始终以本地为准，
#     与打包技能 bootstrap 的"永不覆盖"原则一致）；
#   - 备份（每次 workspace 初始化时，即每条用户消息 intake 的后台任务）：
#     与 R2 侧清单按 sha256 比对，只上传新增/变更文件，并把本地已删除
#     的文件从 R2 同步删除。
# 清单存放在 `skills/{ns}/.manifest.json`（relpath -> sha256），删除语义
# 靠它跨重启传播；该文件名在本前缀下为保留名。R2 未配置时整段同步直接
# 跳过，行为与旧版纯本地完全一致。
_SKILLS_R2_PREFIX = "skills"
_SKILLS_MANIFEST_NAME = ".manifest.json"


def _skills_r2_prefix(namespace: str) -> str:
    return f"{_SKILLS_R2_PREFIX}/{namespace}"


def _is_safe_skill_relpath(rel: str) -> bool:
    """远端清单/备份里的相对路径必须落在 skills/ 内部（防御性校验）。"""
    if not rel or rel == _SKILLS_MANIFEST_NAME or rel.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in Path(rel).parts)


def _scan_skills_dir(skills_dir: Path) -> dict[str, str]:
    """本地 skills/ 目录快照：relpath -> sha256。

    用内容哈希而非 mtime 做比对：恢复写盘后 mtime 必然变化，mtime 方案
    会把整棵树重复上传一遍；哈希方案在无变化时零上传。跳过符号链接，
    避免把链接目标内容误当技能文件备份。
    """
    snapshot: dict[str, str] = {}
    if not skills_dir.is_dir():
        return snapshot
    for path in sorted(skills_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(skills_dir).as_posix()
        if not _is_safe_skill_relpath(rel):
            continue
        if rel == ".packaged-manifest.json":
            continue
        try:
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            logger.warning("skills 文件读取失败，跳过备份 %s: %s", rel, exc)
    return snapshot


async def _load_skills_manifest(namespace: str) -> dict[str, str]:
    """读取 R2 侧清单；缺失/损坏一律按空清单处理（自愈式重建）。"""
    key = f"{_skills_r2_prefix(namespace)}/{_SKILLS_MANIFEST_NAME}"
    data = await download_from_r2(key)
    if data is None:
        return {}
    try:
        files = json.loads(data.decode("utf-8")).get("files")
        if not isinstance(files, dict):
            return {}
        return {
            str(rel): str(digest)
            for rel, digest in files.items()
            if _is_safe_skill_relpath(str(rel))
        }
    except Exception as exc:
        logger.warning("skills 清单解析失败 namespace=%s: %s", namespace, exc)
        return {}


async def _restore_user_skills_from_r2(chat_id: int, home: Path, namespace: str) -> int:
    """从 R2 备份补齐本地缺失的技能文件；返回恢复的文件数。

    只填充缺失文件，绝不覆盖本地已有内容。放在打包技能 bootstrap 之前
    执行：两步都是"只填缺失"，先恢复 R2（用户实际状态）再补打包副本，
    用户改过的包内技能文件不会被 pristine 打包版抢先占位。
    """
    if not is_r2_configured():
        return 0
    prefix = _skills_r2_prefix(namespace)
    keys = await list_r2_objects(prefix)
    if not keys:
        return 0
    skills_dir = workspace_skills_root(chat_id, namespace)
    restored = 0
    for key in keys:
        rel = key[len(prefix) + 1:]
        if not _is_safe_skill_relpath(rel):
            continue
        dst = skills_dir / rel
        if dst.exists():
            continue
        data = await download_from_r2(key)
        if data is None:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        restored += 1
    if restored:
        logger.info(
            "从 R2 恢复用户 skills namespace=%s: %d 个文件", namespace, restored,
        )
    return restored


async def _backup_user_skills_to_r2(chat_id: int, home: Path, namespace: str) -> None:
    """把本地 skills/ 与 R2 清单比对后做增量备份（上传变更、同步删除）。

    无变化时只有一次清单 GET，零写入；首次备份才整树上传。调用方保证
    同一 namespace 串行（workspace init 锁内），无需额外并发控制。
    """
    if not is_r2_configured():
        return
    prefix = _skills_r2_prefix(namespace)
    manifest = await _load_skills_manifest(namespace)
    local = await asyncio.to_thread(_scan_skills_dir, home / "skills")
    changed = {rel: digest for rel, digest in local.items() if manifest.get(rel) != digest}
    removed = [rel for rel in manifest if rel not in local]
    if not changed and not removed:
        return

    skills_dir = home / "skills"
    for rel in sorted(changed):
        data = await asyncio.to_thread((skills_dir / rel).read_bytes)
        await upload_bytes_to_r2(data, f"{prefix}/{rel}", "application/octet-stream")
    for rel in sorted(removed):
        await delete_r2_object(f"{prefix}/{rel}")

    payload = json.dumps({"files": local}, ensure_ascii=False).encode("utf-8")
    await upload_bytes_to_r2(
        payload, f"{prefix}/{_SKILLS_MANIFEST_NAME}", "application/json",
    )
    logger.info(
        "用户 skills 备份到 R2 namespace=%s: 上传 %d、删除 %d",
        namespace, len(changed), len(removed),
    )


async def _ensure_workspace_initialized(chat_id: int, namespace: str | None = None) -> None:
    """Run one-time workspace skill initialization.

    Initialization is protected by a per-workspace lock and a persistent marker
    so repeated calls never re-run the packaged-skill sync.

    v2.3.1 之后：打包 bootstrap 之外新增用户 skills/ 的 R2 持久化通道——
    首次初始化先从 R2 恢复自建技能再补打包副本；每次调用（即每条用户
    消息 intake 的后台任务）都做一次增量备份，把上一回合经 bash/
    text_editor 落盘的技能变更同步到 R2，服务重启/磁盘清空后自动找回。
    """
    resolved_namespace = workspace_namespace(chat_id, namespace)
    key = resolved_namespace
    lock = await _get_workspace_init_lock(key)
    async with lock:
        # workspace 根（agent 家目录本身）才是 skills/ 与初始化标记的归属；
        # agent_home() 首次访问时会自动迁移遗留布局条目（含 v2.3.0 过渡
        # 草案的 claude/ 折叠回根）。
        home = agent_home(chat_id, resolved_namespace)
        marker = home / ".skills_initialized"

        if key in _workspace_initialized or marker.is_file():
            _workspace_initialized.add(key)
        else:
            try:
                # 先恢复 R2 备份（用户实际状态），再补打包技能（只填缺失）。
                await _restore_user_skills_from_r2(
                    chat_id, home, resolved_namespace,
                )

                from skills import sync_all_skill_assets_to_workspace

                summary = await asyncio.to_thread(
                    sync_all_skill_assets_to_workspace,
                    home,
                )
                if summary.get("errors"):
                    logger.warning(
                        "部分 skill 包初始化失败 namespace=%s: %s",
                        resolved_namespace,
                        "; ".join(summary["errors"]),
                    )
                    return

                marker.write_text("initialized\n", encoding="utf-8")
                _workspace_initialized.add(key)
            except Exception as exc:
                logger.warning(
                    "初始化 skill 包到 workspace 失败 namespace=%s: %s",
                    resolved_namespace,
                    exc,
                )
                return

        # 备份通道：与 todo/memory 的定向 R2 同步同型，失败只降级（下次
        # 初始化重试），绝不阻断消息处理。仍在 init 锁内，与首次初始化
        # 串行，避免恢复/备份互相踩踏。
        try:
            await _backup_user_skills_to_r2(chat_id, home, resolved_namespace)
        except Exception as exc:
            logger.warning(
                "备份用户 skills 到 R2 失败 namespace=%s: %s",
                resolved_namespace,
                exc,
            )




# ========== 单文件定向同步（state/ 域） ==========
# 用于 todo / memory 等小 JSON 文件。这些文件的本地路径在 state/{ns}/ 目录，
# R2 key 也在 state/{ns}/ prefix 下，和本地 workspace 隔离。
# 不做文件名黑名单 —— 用户在 workspace 里放同名文件也互不影响。

async def _sync_named_file_from_r2(chat_id: int, local_path: Path, remote_name: str) -> None:
    """
    从 R2 的 state/{ns}/{remote_name} 下载到 local_path。
    如果 R2 上没有该文件，本地保留现状（可能是首次创建）。
    """
    safe_name = os.path.normpath(remote_name)
    if safe_name == "." or safe_name.startswith("..") or os.path.isabs(safe_name):
        logger.warning(f"拒绝路径遍历的 remote_name: {remote_name!r}")
        return

    local_path.parent.mkdir(parents=True, exist_ok=True)
    key = f"state/{workspace_namespace(chat_id)}/{safe_name}"
    data = await download_from_r2(key)
    if data is not None:
        with open(local_path, "wb") as f:
            f.write(data)


async def _sync_named_file_to_r2(chat_id: int, local_path: Path, remote_name: str) -> None:
    """
    将 local_path 上传到 R2 的 state/{ns}/{remote_name}。
    如果本地文件不存在，则删除 R2 上的对应文件。
    """
    safe_name = os.path.normpath(remote_name)
    if safe_name == "." or safe_name.startswith("..") or os.path.isabs(safe_name):
        logger.warning(f"拒绝路径遍历的 remote_name: {remote_name!r}")
        return

    key = f"state/{workspace_namespace(chat_id)}/{safe_name}"
    if local_path.is_file():
        with open(local_path, "rb") as f:
            data = f.read()
        await upload_bytes_to_r2(data, key, "application/json")
    else:
        await delete_r2_object(key)


# ========== upload/ & download/ 说明 ==========
# upload/ 与 download/ 是 workspace 根目录的两棵子树：
#   - download/：用户上传文档的本地落地缓冲（Telegram → R2 缓存 → 本地）；
#   - upload/：待发送产物的暂存区（present_files 只接受 upload/ 下的文件）。
# bash 本就能直接读写这两棵子树（相对路径即可），无需跨边界原语：
# 模型直接使用 bash：`cat download/x.pdf`、`cp out.txt upload/out.txt`。
#
# 注意：download/ 不做 R2 同步，持久化由 file_handlers.py 的
# `telegram/{file_id}` R2 缓存负责；upload/ 也不做 R2 镜像同步——
# 恢复方向从未被调用且会造成重复存储。


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
