"""execute_present_files：把 workspace 文件作为附件发送到聊天（自 tool_executors.py 拆出）。"""

import os
import json
import asyncio
from pathlib import Path
from typing import List

import aiohttp

from config import BASE_URL
from workspace_paths import (
    workspace_upload_root, workspace_workdir,
)
from workspace_utils import _get_workspace_lock, _ensure_runtime_workspace
from chat_actions import chat_action_scope

import logging

logger = logging.getLogger(__name__)


async def execute_present_files(chat_id: int, paths: List[str], namespace: str | None = None) -> str:
    """Send staged files from the workspace to the chat as attachments.

    Paths are workspace-relative. Files MUST be staged under ``upload/``
    first, for example ``cp out.txt upload/out.txt``; the corresponding call
    is ``present_files([\"upload/out.txt\"])``. Absolute paths are accepted
    only when they resolve inside this chat's workspace. The final resolved
    file must remain inside ``upload/``. This keeps Bash, file tools, and file
    presentation in one path namespace.
    """
    if not paths:
        return json.dumps({
            "sent": [],
            "failed": [],
            "error": "No paths provided. Files must be staged under upload/ first.",
        })
    # ★ init 在 workspace lock 外面执行（同 bash / text_editor）。
    # 显式接收 namespace：与 bash/text_editor 一致，避免依赖 ContextVar
    # 在 background task 里解析到错误的 namespace。
    await _ensure_runtime_workspace(chat_id, namespace)

    lock = await _get_workspace_lock(chat_id)
    async with lock:
        upload_root = workspace_upload_root(chat_id, namespace)
        sent = []
        failed = []
        # 文件大小上限：50MB，防止 OOM
        _MAX_PRESENT_FILE_SIZE = 50 * 1024 * 1024
        # 提升：把 aiohttp session 提升到循环外层，避免每个文件都做一次
        # TLS 握手。同时若异常 str(e) 里包含了带 TELEGRAM_BOT_TOKEN 的 URL
        # （BASE_URL 里嵌了 bot token），截断 + 脱敏后再写入 failed 列表，
        # 否则这个 list 会被 LLM 看到从而泄露 token。
        timeout = aiohttp.ClientTimeout(total=60)
        # 在循环外解析一次 upload_root，避免每个文件都重新 resolve。
        try:
            upload_resolved = upload_root.resolve()
        except Exception:
            logger.debug("execute_present_files 内部忽略的异常", exc_info=True)
            upload_resolved = upload_root
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for path in paths:
                if not isinstance(path, str) or not path:
                    failed.append(f"{path} (invalid path)")
                    continue
                # 拒绝嵌入的 null 字节
                if "\x00" in path:
                    failed.append(f"{path} (invalid path)")
                    continue

                # ----- 统一 workspace-relative 路径解析 -----
                # 所有相对路径都相对于唯一 workspace 根目录解析；不再把
                # present_files 的参数解释成相对于 upload/ 的第二套命名空间。
                raw_path = path.strip()
                while raw_path.startswith("./"):
                    raw_path = raw_path[2:]
                workspace = workspace_workdir(chat_id, namespace).resolve()
                try:
                    if os.path.isabs(raw_path):
                        candidate = Path(raw_path).expanduser()
                        display_path = str(candidate.resolve().relative_to(workspace))
                    else:
                        norm = os.path.normpath(raw_path)
                        if norm in ("", ".") or norm == ".." or norm.startswith(".." + os.sep):
                            raise ValueError("path escapes workspace")
                        display_path = norm
                        candidate = workspace / norm
                    resolved = candidate.resolve()
                except (OSError, ValueError):
                    failed.append(f"{path} (invalid workspace-relative path)")
                    continue
                if resolved != upload_resolved and upload_resolved not in resolved.parents:
                    failed.append(
                        f"{path} (not staged: workspace-relative path must be under "
                        f"upload/, for example upload/{Path(display_path).name})"
                    )
                    continue

                if not resolved.is_file():
                    failed.append(
                        f"{path} (file not found at workspace path {display_path!r}; "
                        f"stage it from workspace root with `cp {display_path} "
                        f"upload/{Path(display_path).name}` and call present_files with "
                        f"the workspace-relative path `upload/{Path(display_path).name}`)"
                    )
                    continue
                try:
                    file_size = resolved.stat().st_size
                    if file_size > _MAX_PRESENT_FILE_SIZE:
                        failed.append(f"{path} (file too large: {file_size} bytes)")
                        continue
                    # 使用 asyncio.to_thread 包装同步 read，避免阻塞事件循环
                    file_data = await asyncio.to_thread(resolved.read_bytes)
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(chat_id))
                    form.add_field("document", file_data, filename=resolved.name)
                    # chat action：bot 正在发送文件（sendDocument）。每个文件的
                    # 上传期间显示 upload_document；多文件连续发送时引用计数
                    # 叠加，指示无断档；上传超 5 秒由 4 秒循环重发保活。
                    async with chat_action_scope(chat_id, "upload_document"):
                        async with session.post(f"{BASE_URL}/sendDocument", data=form) as resp:
                            if resp.status == 200:
                                sent.append(resolved.name)
                            else:
                                failed.append(f"{path} (send failed: HTTP {resp.status})")
                except aiohttp.ClientError as e:
                    # 网络层错误：str(e) 可能含 URL（带 bot token），脱敏后再写。
                    safe_msg = str(e)
                    if BASE_URL and BASE_URL in safe_msg:
                        safe_msg = "[redacted url]"
                    failed.append(f"{path} (network error: {safe_msg[:80]})")
                except Exception as e:
                    # 通用兜底：同样脱敏 URL，避免 token 泄露给 LLM 上下文。
                    logger.debug("execute_present_files 内部忽略的异常", exc_info=True)
                    safe_msg = str(e)
                    if BASE_URL and BASE_URL in safe_msg:
                        safe_msg = "[redacted url]"
                    failed.append(f"{path} (error: {safe_msg[:50]})")
        # 返回结构：{"sent": [...], "failed": [...]}；仅当有真实错误时才附带
        # "error" 键。成功路径不再输出 "error": null —— 对模型而言是零信息
        # 字段，且会诱使模型在回复里重复说明“没有错误”。
        return json.dumps({"sent": sent, "failed": failed})


# ---------- 已移除工具的迁移提示 ----------
# stage_upload / fetch_download / list_download / list_upload 已删除：upload/
# 与 download/ 本就是工作区根目录的子目录，bash 可直接读写；所有文件工具
# 的相对路径也以 workspace 根目录解析（`cat download/x.pdf`、
# `cp out.txt upload/out.txt`、`present_files(["upload/out.txt"])`）。
# 若模型（尤其是带着旧对话历史）仍调用旧工具，返回可操作的迁移指引
# 而不是干巴巴的“未知工具”。
_REMOVED_TOOL_HINTS = {
    "fetch_download": (
        "fetch_download 已移除：download/ 就在工作区根目录下，可直接访问。"
        "用 bash（如 `ls download/`、`cat download/<文件名>`）或 text_editor"
        "（path 填 `download/<文件名>`）直接读取即可。"
    ),
    "stage_upload": (
        "stage_upload 已移除：用 bash 把文件复制到 upload/ 子目录即可，"
        "例如 `cp <文件> upload/<文件名>`，然后调用 present_files([\"upload/<文件名>\"]) 发送给用户。"
    ),
    "list_download": (
        "list_download 已移除：用 bash 执行 `ls -la download/` 查看用户上传的文件。"
    ),
    "list_upload": (
        "list_upload 已移除：用 bash 执行 `ls -la upload/` 查看发送暂存区里的文件。"
    ),
    "ip_geo": (
        "ip_geo 已移除：不再提供 IP 归属地查询能力，无需重试。"
    ),
    "send_message_to_user": (
        "send_message_to_user 已移除：请改用 message_user（提问/留言，超时即用户不在）"
        "或 deliver_reply（静默模式下交付最终回复）。无需重试本工具。"
    ),
    "ask_user": (
        "ask_user 已更名为 message_user：参数与行为兼容（question 必填，options 可选），"
        "请改用 message_user。无需重试本工具。"
    ),
}
