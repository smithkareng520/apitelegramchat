from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path


_NAMESPACE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_WORKDIR_NAME = os.getenv("APITELEGRAMCHAT_WORKDIR_NAME", "workspace").strip() or "workspace"
_STATE_DIR_NAME = os.getenv("APITELEGRAMCHAT_STATE_DIR_NAME", "state").strip() or "state"
_RUNTIME_DIR_NAME = os.getenv("APITELEGRAMCHAT_RUNTIME_DIR_NAME", "runtime").strip() or "runtime"
_SKILLS_DIR_NAME = os.getenv("APITELEGRAMCHAT_SKILLS_DIR_NAME", "skills").strip() or "skills"
_UPLOAD_DIR_NAME = os.getenv("APITELEGRAMCHAT_UPLOAD_DIR_NAME", "upload").strip() or "upload"
_DOWNLOAD_DIR_NAME = os.getenv("APITELEGRAMCHAT_DOWNLOAD_DIR_NAME", "download").strip() or "download"


def _resolved_namespace(chat_id: object, namespace: object | None = None) -> str:
    if namespace is not None:
        return sanitize_namespace(namespace)
    try:
        from apitelegramchat.state import get_current_user_namespace

        current = get_current_user_namespace()
        if current:
            return sanitize_namespace(current)
    except Exception:
        pass
    return sanitize_namespace(chat_id)


def _secure_directory(path: Path) -> Path:
    """Create a private runtime directory without accepting a final symlink."""
    expanded = path.expanduser()
    if expanded.exists() and expanded.is_symlink():
        raise RuntimeError(f"Refusing symlinked runtime directory: {expanded}")
    expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = expanded.resolve()
    if resolved.is_symlink() or not resolved.is_dir():
        raise RuntimeError(f"Invalid runtime directory: {expanded}")
    try:
        os.chmod(resolved, 0o700)
    except OSError as exc:
        raise RuntimeError(f"Unable to protect runtime directory {resolved}: {exc}") from exc
    return resolved


@lru_cache(maxsize=1)
def data_root() -> Path:
    """Return the private root for runtime state and workspaces."""
    base = os.getenv("APITELEGRAMCHAT_DATA_DIR", "/tmp/apitelegramchat_data")
    return _secure_directory(Path(base))


def sanitize_namespace(value: object) -> str:
    raw = "default" if value is None else str(value).strip()
    raw = raw or "default"
    safe = _NAMESPACE_RE.sub("_", raw)
    return safe.strip("._") or "default"


def workspace_root(chat_id: object, namespace: object | None = None) -> Path:
    ns = _resolved_namespace(chat_id, namespace)
    parent = _secure_directory(data_root() / "workspaces")
    return _secure_directory(parent / ns)




def workspace_workdir(chat_id: object, namespace: object | None = None) -> Path:
    """Use the user workspace root directly as the agent working directory.

    The workspace is local-only and is never mirrored wholesale to R2. Packaged
    skills live under ``skills/``; runtime/ remains available for local caches.
    """
    root = workspace_root(chat_id, namespace)
    root.mkdir(parents=True, exist_ok=True)
    workspace_skills_root(chat_id, namespace)
    return root.resolve()


def workspace_file(chat_id: object, filename: str, namespace: object | None = None) -> Path:
    return workspace_root(chat_id, namespace) / filename


def state_root() -> Path:
    return _secure_directory(data_root() / _STATE_DIR_NAME)


def chat_state_root(chat_id: object, namespace: object | None = None) -> Path:
    ns = _resolved_namespace(chat_id, namespace)
    return _secure_directory(state_root() / ns)


def state_file(chat_id: object, filename: str, namespace: object | None = None) -> Path:
    return chat_state_root(chat_id, namespace) / filename


def memory_state_file(chat_id: object, namespace: object | None = None) -> Path:
    return state_file(chat_id, "memories.json", namespace)


def todo_state_file(chat_id: object, namespace: object | None = None) -> Path:
    return state_file(chat_id, "todos.json", namespace)


def workspace_namespace(chat_id: object, namespace: object | None = None) -> str:
    """Return the canonical workspace namespace for this tool invocation.

    Callers that coordinate multiple tools should resolve this once and pass the
    returned value explicitly to every workspace operation. This avoids relying on
    the request ContextVar repeatedly across async tasks/subtasks.
    """
    return _resolved_namespace(chat_id, namespace)


def workspace_identity(chat_id: object, namespace: object | None = None) -> tuple[str, Path]:
    """Resolve the stable namespace and root together.

    The tuple is intentionally cheap to pass through tool dispatch so file_editor,
    bash, skill sync, and persistence all operate on the exact same filesystem tree.
    """
    resolved = workspace_namespace(chat_id, namespace)
    return resolved, workspace_root(chat_id, resolved)


def runtime_cache_root(chat_id: object, namespace: object | None = None) -> Path:
    """持久化运行时目录，完全独立于用户文件同步层。"""
    return _secure_directory(workspace_root(chat_id, namespace) / _RUNTIME_DIR_NAME)

def workspace_skills_root(chat_id: object, namespace: object | None = None) -> Path:
    """本地 skill 资源层，不参与用户文件同步。"""
    return _secure_directory(workspace_root(chat_id, namespace) / _SKILLS_DIR_NAME)


def workspace_upload_root(chat_id: object, namespace: object | None = None) -> Path:
    """Staging area for files the model wants to send to the user.

    This directory is the sole source for `present_files`. The model must
    explicitly stage artifacts here (via bash `cp`/redirect or via the
    `stage_upload` tool) before they can be attached to a chat message.

    Bash is allowed to read/write files here through relative paths
    (`../upload/<name>`), but the sandbox refuses to `cd` into this tree
    or execute any command while the cwd is inside it. This prevents
    package managers / build tools from polluting the staging area.
    """
    return _secure_directory(workspace_root(chat_id, namespace) / _UPLOAD_DIR_NAME)


def workspace_download_root(chat_id: object, namespace: object | None = None) -> Path:
    """Landing area for files the user uploaded via Telegram.

    When a user sends a document and the active model does not support
    native document input, the file is saved here (not into files/).
    The model can list this directory and explicitly fetch files into
    its local workspace via the `fetch_download`
    tool before working on them.

    Bash is allowed to read files here (`../download/<name>`), but the
    sandbox refuses to `cd` into this tree or execute any command while
    the cwd is inside it. This keeps user-supplied files immutable from
    the model's execution perspective.
    """
    return _secure_directory(workspace_root(chat_id, namespace) / _DOWNLOAD_DIR_NAME)


def is_inside_upload_or_download(path: object) -> bool:
    """Return True if *path* resolves inside any chat's upload/ or download/ tree.

    Used by the bash sandbox to refuse execution while cwd is inside one
    of these staging directories. The check is intentionally conservative:
    it walks the parent chain looking for a directory whose name matches
    the upload/download dir name AND whose parent looks like a workspace
    root (i.e. lives under data_root()/workspaces).
    """
    try:
        resolved = Path(path).expanduser().resolve() if path is not None else None
    except Exception:
        return False
    if resolved is None:
        return False
    try:
        ws_root = data_root() / "workspaces"
        ws_resolved = ws_root.resolve()
    except Exception:
        return False
    # Walk up: if any ancestor is named upload/ or download/ AND that
    # ancestor's parent is itself under workspaces/, we're inside.
    target_names = {_UPLOAD_DIR_NAME, _DOWNLOAD_DIR_NAME}
    current = resolved
    for _ in range(32):  # bounded climb to avoid pathological loops
        if current.name in target_names:
            parent = current.parent
            if parent == ws_resolved or ws_resolved in parent.parents:
                return True
        if current == current.parent:
            break
        current = current.parent
    return False
