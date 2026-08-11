from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path


_NAMESPACE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_WORKDIR_NAME = os.getenv("APITELEGRAMCHAT_WORKDIR_NAME", "workspace").strip() or "workspace"
_STATE_DIR_NAME = os.getenv("APITELEGRAMCHAT_STATE_DIR_NAME", "state").strip() or "state"
_FILES_DIR_NAME = os.getenv("APITELEGRAMCHAT_FILES_DIR_NAME", "files").strip() or "files"
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
    return sanitize_namespace(f"chat_{chat_id}")


@lru_cache(maxsize=1)
def data_root() -> Path:
    # Use a writable location by default. On managed deploys the app directory may
    # be read-only for the runtime user, so default to /tmp unless explicitly set.
    base = os.getenv("APITELEGRAMCHAT_DATA_DIR", "/tmp/apitelegramchat_data")
    root = Path(base).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        fallback = Path("/tmp/apitelegramchat_data").resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        root = fallback
    return root


def sanitize_namespace(value: object) -> str:
    raw = "default" if value is None else str(value).strip()
    raw = raw or "default"
    safe = _NAMESPACE_RE.sub("_", raw)
    return safe.strip("._") or "default"


def workspace_root(chat_id: object, namespace: object | None = None) -> Path:
    ns = _resolved_namespace(chat_id, namespace)
    root = data_root() / "workspaces" / ns
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def workspace_files_root(chat_id: object, namespace: object | None = None) -> Path:
    """User-owned files only. This is the sole directory mirrored to R2."""
    root = workspace_root(chat_id, namespace) / _FILES_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def workspace_workdir(chat_id: object, namespace: object | None = None) -> Path:
    """Ephemeral execution working directory, deliberately outside the R2 persistence tree.

    The agent shell works on a sandbox-local copy under runtime/exec. Only explicit
    persistence operations copy selected files into files/ and R2. This prevents
    package managers, build systems, caches, and generated dependency trees from
    ever becoming part of the persistence boundary.
    """
    root = runtime_cache_root(chat_id, namespace) / "exec"
    root.mkdir(parents=True, exist_ok=True)

    # Skill instructions historically use ../skills/<skill_id> from the shell cwd.
    # Keep that path stable without placing skills inside the persistent files tree.
    bridge = root.parent / "skills"
    target = workspace_skills_root(chat_id, namespace)
    try:
        if bridge.is_symlink():
            if bridge.resolve() != target.resolve():
                bridge.unlink()
        elif bridge.exists():
            # A real directory here is runtime-owned state; leave it alone rather
            # than deleting user data.
            target = None
        if target is not None and not bridge.exists():
            bridge.symlink_to(target, target_is_directory=True)
    except OSError:
        # The sandbox can still operate without the convenience bridge; callers can
        # use an absolute/known skill path when necessary.
        pass
    return root.resolve()


def workspace_file(chat_id: object, filename: str, namespace: object | None = None) -> Path:
    return workspace_files_root(chat_id, namespace) / filename


def state_root() -> Path:
    root = data_root() / _STATE_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def chat_state_root(chat_id: object, namespace: object | None = None) -> Path:
    ns = _resolved_namespace(chat_id, namespace)
    root = state_root() / ns
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def state_file(chat_id: object, filename: str, namespace: object | None = None) -> Path:
    return chat_state_root(chat_id, namespace) / filename


def memory_state_file(chat_id: object, namespace: object | None = None) -> Path:
    return state_file(chat_id, "memories.json", namespace)


def todo_state_file(chat_id: object, namespace: object | None = None) -> Path:
    return state_file(chat_id, "todos.json", namespace)


def workspace_namespace(chat_id: object, namespace: object | None = None) -> str:
    return _resolved_namespace(chat_id, namespace)


def runtime_cache_root(chat_id: object, namespace: object | None = None) -> Path:
    """持久化运行时目录，完全独立于用户文件同步层。"""
    root = workspace_root(chat_id, namespace) / _RUNTIME_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()

def workspace_skills_root(chat_id: object, namespace: object | None = None) -> Path:
    """本地 skill 资源层，不参与用户文件同步。"""
    root = workspace_root(chat_id, namespace) / _SKILLS_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


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
    root = workspace_root(chat_id, namespace) / _UPLOAD_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def workspace_download_root(chat_id: object, namespace: object | None = None) -> Path:
    """Landing area for files the user uploaded via Telegram.

    When a user sends a document and the active model does not support
    native document input, the file is saved here (not into files/).
    The model can list this directory and explicitly fetch files into
    its ephemeral workspace (`runtime/exec/`) via the `fetch_download`
    tool before working on them.

    Bash is allowed to read files here (`../download/<name>`), but the
    sandbox refuses to `cd` into this tree or execute any command while
    the cwd is inside it. This keeps user-supplied files immutable from
    the model's execution perspective.
    """
    root = workspace_root(chat_id, namespace) / _DOWNLOAD_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


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
