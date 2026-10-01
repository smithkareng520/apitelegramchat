"""execute_present_files：把 upload/ 暂存区文件作为附件发送到聊天（自 tool_executors.py 拆出）。"""

import os
import json
import asyncio
from pathlib import Path
from typing import List

import aiohttp

from config import BASE_URL
from workspace_paths import (
    workspace_upload_root,
    workspace_workdir,
)
from workspace_utils import _get_workspace_lock, _ensure_runtime_workspace
from chat_actions import chat_action_scope

import logging

logger = logging.getLogger(__name__)


# 模型可见的 host 内建工具定义。执行实现仍由 execute_present_files 提供，
# 此 schema 负责把工具正式暴露到 tool_registry -> get_model_tools()。
PRESENT_FILES_TOOL = {
    "type": "function",
    "function": {
        "name": "present_files",
        "description": (
            "Send one or more files to the user as Telegram document attachments. "
            "Only files under the current workspace's upload/ directory can be sent. "
            "First use bash to copy or create the file under upload/, then call this tool "
            "with workspace-relative paths such as upload/report.pdf. "
            "Do not use absolute paths or paths outside upload/."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "description": "Workspace-relative paths of files under upload/ to send to the user."
                }
            },
            "required": ["paths"],
            "additionalProperties": False
        },
        "input_examples": [
            {"paths": ["upload/report.pdf"]},
            {"paths": ["upload/report.pdf", "upload/data.csv"]}
        ]
    }
}


async def execute_present_files(chat_id: int, paths: List[str], namespace: str | None = None) -> str:
    """Send files from the ``upload/`` staging directory to the chat.

    Only files under this chat's ``upload/`` directory can be presented.
    Stage outputs there first via bash (e.g. ``cp out.txt upload/out.txt``),
    then pass workspace-relative paths such as ``upload/out.txt``. Absolute
    paths are accepted only when they resolve inside ``upload/``. This keeps
    Bash, file tools, and file presentation in one path namespace.
    """
    if not paths:
        return json.dumps({
            "sent": [],
            "failed": [],
            "error": "No paths provided.",
        })
    # ★ init 在 workspace lock 外面执行（同 bash / text_editor）。
    # 显式接收 namespace：与 bash/text_editor 一致，避免依赖 ContextVar
    # 在 background task 里解析到错误的 namespace。
    await _ensure_runtime_workspace(chat_id, namespace)

    lock = await _get_workspace_lock(chat_id)
    async with lock:
        sent = []
        failed = []
        # 文件大小上限：50MB，防止 OOM
        _MAX_PRESENT_FILE_SIZE = 50 * 1024 * 1024
        # 提升：把 aiohttp session 提升到循环外层，避免每个文件都做一次
        # TLS 握手。同时若异常 str(e) 里包含了带 TELEGRAM_BOT_TOKEN 的 URL
        # （BASE_URL 里嵌了 bot token），截断 + 脱敏后再写入 failed 列表，
        # 否则这个 list 会被 LLM 看到从而泄露 token。
        timeout = aiohttp.ClientTimeout(total=60)
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
                # 所有相对路径都相对于唯一 workspace 根目录解析。
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
                # 工作区边界：解析后必须仍位于本 chat 的 workspace 内
                # （与 bash / text_editor 的 Landlock 边界一致）。
                if resolved != workspace and workspace not in resolved.parents:
                    failed.append(f"{path} (path escapes workspace)")
                    continue
                # 发送边界：present_files 只发送 upload/ 暂存区里的文件。
                # 其他位置的文件一律拒绝，并提示先把文件复制进 upload/。
                upload_root = workspace_upload_root(chat_id, namespace).resolve()
                if resolved != upload_root and upload_root not in resolved.parents:
                    failed.append(
                        f"{path} (not under upload/: stage it first, e.g. `cp {display_path} upload/`)"
                    )
                    continue

                if not resolved.is_file():
                    failed.append(
                        f"{path} (file not found at workspace path {display_path!r})"
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


