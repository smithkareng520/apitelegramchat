# todo_tool.py
"""
任务 / 待办清单工具。

设计目标
--------
1. 给 AI agent 一个轻量、持久的任务管理能力——支持新增、列表、完成、
   反完成、删除、清空、编辑、改优先级。
2. 数据按用户隔离，落在 ./state/{user_id}/todos.json，复用既有
   workspace_utils 的 R2 同步链路，无需额外存储。
3. 给 Agent 工具结果提供富文本渲染：状态 emoji、优先级、删除线、可折叠统计区。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
import uuid
from pathlib import Path
from apitelegramchat.workspace_paths import todo_state_file
from apitelegramchat.token_budget import truncate_to_token_budget
from typing import Any, Optional


from apitelegramchat.workspace_utils import (
    _get_workspace_lock,
    _sync_named_file_from_r2,
    _sync_named_file_to_r2,
)

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
TODO_FILENAME = "todos.json"
VALID_PRIORITIES = ("low", "medium", "high")
VALID_FILTERS = ("all", "pending", "done")
TODO_TITLE_TOKEN_BUDGET = 200
TODO_TAG_TOKEN_BUDGET = 24
TODO_NOTE_TOKEN_BUDGET = 500
MAX_TODOS = 500  # 单 chat 上限，防止失控增长
MAX_TAGS = 8

PRIORITY_META = {
    "high":   {"emoji": "🔴", "label": "高", "weight": 3},
    "medium": {"emoji": "🟡", "label": "中", "weight": 2},
    "low":    {"emoji": "🟢", "label": "低", "weight": 1},
}


# ---------- 存储层 ----------
def _todo_path(chat_id: int) -> Path:
    return todo_state_file(chat_id)


def _new_id() -> str:
    """8 位短 id，足够避免单 chat 内冲突。"""
    return uuid.uuid4().hex[:8]


def _empty_store() -> dict:
    return {"todos": [], "updated_at": 0}


def _load_local(chat_id: int) -> dict:
    """从本地读取 todos.json。文件不存在或损坏时返回空 store。"""
    path = _todo_path(chat_id)
    if not path.is_file():
        return _empty_store()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("todos"), list):
            return _empty_store()
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"todos.json 读取失败 (chat={chat_id}): {e}")
        return _empty_store()


def _save_local(chat_id: int, store: dict) -> None:
    """以原子方式写入本地，并保证目录存在。

    修复：之前所有并发 writer 共用 ``todos.json.tmp`` 这一个 tmp 名，
    后写的覆盖先写的，导致并发 todo 操作丢数据。现在 tmp 名加 PID + 随机后缀。
    """
    path = _todo_path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = int(time.time())
    tmp = path.with_suffix(f".json.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _find_todo(todos: list, todo_id: str) -> tuple[int, dict] | None:
    """返回 (index, todo) 或 None。"""
    if not todo_id:
        return None
    target = str(todo_id).lstrip("#")
    for i, t in enumerate(todos):
        if str(t.get("id", "")).lstrip("#") == target:
            return i, t
    return None


def _normalize_priority(value: Optional[str]) -> str:
    if not value:
        return "medium"
    v = str(value).strip().lower()
    if v in VALID_PRIORITIES:
        return v
    # 容错：常见别名
    alias = {"p0": "high", "p1": "high", "p2": "medium", "p3": "low",
             "高": "high", "中": "medium", "低": "low"}
    return alias.get(v, "medium")


def _normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        # 逗号或空格分隔
        parts = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    elif isinstance(tags, list):
        parts = [str(t).strip() for t in tags if str(t).strip()]
    else:
        return []
    # 去重 + token 预算
    seen = set()
    out = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(truncate_to_token_budget(p, TODO_TAG_TOKEN_BUDGET, suffix="…"))
        if len(out) >= MAX_TAGS:
            break
    return out


# ---------- 业务逻辑 ----------
async def _read_store(chat_id: int, fn) -> dict:
    """
    读取型操作：先从 R2 拉取最新内容到本地，再执行读取，不回写 store。
    """
    lock = await _get_workspace_lock(chat_id)
    async with lock:
        try:
            await _sync_named_file_from_r2(chat_id, _todo_path(chat_id), TODO_FILENAME)
        except Exception as e:
            logger.warning(f"todos: R2→local 同步失败 (chat={chat_id}): {e}")
        store = _load_local(chat_id)
        try:
            _store, payload = fn(store)
        except _TodoError as e:
            return {"ok": False, "error": str(e), "code": e.code}
        return payload


async def _mutate(chat_id: int, fn) -> dict:
    """
    在 workspace 锁保护下：从 R2 同步 → 加载 → 调用 fn(store) → 保存 → 同步回 R2。
    fn 返回 (store, payload)，payload 是返回给调用方的结构化结果。

    性能优化：只同步 todos.json 单个文件（而非全量 workspace），
    避免当 workspace 有大量文件时每次调用等 30+ 秒。
    """
    lock = await _get_workspace_lock(chat_id)
    async with lock:
        try:
            await _sync_named_file_from_r2(chat_id, _todo_path(chat_id), TODO_FILENAME)
        except Exception as e:
            logger.warning(f"todos: R2→local 同步失败 (chat={chat_id}): {e}")
        store = _load_local(chat_id)
        try:
            store, payload = fn(store)
        except _TodoError as e:
            return {"ok": False, "error": str(e), "code": e.code}
        _save_local(chat_id, store)
        # 同步回 R2（单文件，同步等待——JSON 文件很小，<1s）
        try:
            await _sync_named_file_to_r2(chat_id, _todo_path(chat_id), TODO_FILENAME)
        except Exception as e:
            logger.warning(f"todos: local→R2 同步失败 (chat={chat_id}): {e}")
        return payload


class _TodoError(Exception):
    """业务级错误，会被 _mutate 捕获并转成结构化 error。"""

    def __init__(self, message: str, code: str = "todo_error"):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------- 各操作的实现 ----------
def _normalize_due_at(value: Optional[str]) -> Optional[str]:
    """规范化可选截止时间；保留 ISO 8601 字符串，便于模型和日志判断。"""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise _TodoError("due_at 必须是 ISO 8601 时间，例如 2026-08-29T18:00:00-07:00", "bad_due_at")
    if dt.tzinfo is None:
        # 无时区时按服务器本地时间保存；模型通常会根据当前时间提示词理解用户时区。
        return dt.isoformat(timespec="minutes")
    return dt.isoformat(timespec="minutes")


def _due_status(due_at: Optional[str]) -> str:
    if not due_at:
        return "none"
    try:
        dt = datetime.fromisoformat(str(due_at).replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        delta = (dt - now).total_seconds()
        if delta < 0:
            return "overdue"
        if delta <= 24 * 3600:
            return "due_soon"
        return "upcoming"
    except Exception:
        logger.debug("_due_status 内部忽略的异常", exc_info=True)
        return "unknown"


def _op_add(store: dict, title: str, priority: str, tags: list[str], note: Optional[str], due_at: Optional[str]) -> dict:
    title = (title or "").strip()
    if not title:
        raise _TodoError("title 不能为空", "empty_title")
    title = truncate_to_token_budget(title, TODO_TITLE_TOKEN_BUDGET, suffix="…")
    if len(store["todos"]) >= MAX_TODOS:
        raise _TodoError(f"待办数量已达上限 {MAX_TODOS}，请先清理", "too_many")

    due_at = _normalize_due_at(due_at)
    todo = {
        "id": _new_id(),
        "title": title,
        "done": False,
        "priority": _normalize_priority(priority),
        "tags": _normalize_tags(tags),
        "note": truncate_to_token_budget((note or "").strip(), TODO_NOTE_TOKEN_BUDGET, suffix="…") if note else "",
        "due_at": due_at,
        "created_at": int(time.time()),
        "completed_at": None,
    }
    store["todos"].append(todo)
    payload = {
        "ok": True,
        "action": "add",
        "todo": _todo_summary(todo),
        "total": len(store["todos"]),
        "pending": sum(1 for t in store["todos"] if not t["done"]),
    }
    return store, payload


def _op_list(store: dict, filter_: str, tag: Optional[str], priority: Optional[str]) -> dict:
    todos = store["todos"]
    # 默认排序：未完成在前，再按优先级降序，再按创建时间升序。
    # 展示顺序完全由 created_at + id 决定，不依赖可见编号。
    def sort_key(t: dict):
        return (
            1 if t.get("done") else 0,
            # 安全修复：PRIORITY_META.get(...) 默认 {} 在 store 含有
            # 未经验证的 priority 字段（旧数据 / LLM typo / 手改 JSON）
            # 时会触发 KeyError，让整个 execute_todo 工具直接抛异常。
            # 改为 .get("weight", 2) 链式兜底，确保任何 priority 值都
            # 能落到一个稳定排序权重上。
            -PRIORITY_META.get(t.get("priority", "medium"), {}).get("weight", 2),
            t.get("created_at", 0),
            t.get("id", ""),
        )
    todos_sorted = sorted(todos, key=sort_key)

    filtered = []
    for t in todos_sorted:
        if filter_ == "done" and not t.get("done"):
            continue
        if filter_ == "pending" and t.get("done"):
            continue
        if tag and tag not in t.get("tags", []):
            continue
        if priority and t.get("priority") != _normalize_priority(priority):
            continue
        filtered.append(t)

    payload = {
        "ok": True,
        "action": "list",
        "filter": filter_,
        "tag": tag,
        "priority": priority,
        "todos": [_todo_summary(t) for t in filtered],
        "total": len(todos),
        "pending": sum(1 for t in todos if not t.get("done")),
        "done": sum(1 for t in todos if t.get("done")),
    }
    return store, payload


def _op_toggle(store: dict, todo_id: str, force: Optional[bool]) -> dict:
    found = _find_todo(store["todos"], todo_id)
    if not found:
        raise _TodoError(f"找不到 id 为 {todo_id} 的待办", "not_found")
    _idx, todo = found
    new_state = (not todo["done"]) if force is None else bool(force)
    if todo["done"] == new_state:
        # 状态未变
        return store, {
            "ok": True,
            "action": "toggle",
            "todo": _todo_summary(todo),
            "changed": False,
            "total": len(store["todos"]),
            "pending": sum(1 for t in store["todos"] if not t["done"]),
        }
    todo["done"] = new_state
    todo["completed_at"] = int(time.time()) if new_state else None
    return store, {
        "ok": True,
        "action": "toggle",
        "todo": _todo_summary(todo),
        "changed": True,
        "total": len(store["todos"]),
        "pending": sum(1 for t in store["todos"] if not t["done"]),
    }


def _op_delete(store: dict, todo_id: str) -> dict:
    found = _find_todo(store["todos"], todo_id)
    if not found:
        raise _TodoError(f"找不到 id 为 {todo_id} 的待办", "not_found")
    idx, todo = found
    store["todos"].pop(idx)
    return store, {
        "ok": True,
        "action": "delete",
        "todo": _todo_summary(todo),
        "total": len(store["todos"]),
        "pending": sum(1 for t in store["todos"] if not t["done"]),
    }


def _op_clear(store: dict, filter_: str) -> dict:
    """清空：done=只清已完成；all=清全部。"""
    if filter_ == "done":
        before = len(store["todos"])
        store["todos"] = [t for t in store["todos"] if not t.get("done")]
        removed = before - len(store["todos"])
        msg = f"已清空 {removed} 条已完成待办"
    elif filter_ == "all":
        removed = len(store["todos"])
        store["todos"] = []
        msg = f"已清空全部 {removed} 条待办"
    else:
        raise _TodoError(f"不支持的 clear 过滤: {filter_}", "bad_filter")
    return store, {
        "ok": True,
        "action": "clear",
        "filter": filter_,
        "removed": removed,
        "message": msg,
        "total": len(store["todos"]),
        "pending": sum(1 for t in store["todos"] if not t["done"]),
    }


def _op_edit(store: dict, todo_id: str, title: Optional[str],
             priority: Optional[str], tags: Any, note: Optional[str],
             due_at: Optional[str]) -> dict:
    found = _find_todo(store["todos"], todo_id)
    if not found:
        raise _TodoError(f"找不到 id 为 {todo_id} 的待办", "not_found")
    _idx, todo = found
    changed = []
    if title is not None:
        t = title.strip()
        if not t:
            raise _TodoError("title 不能为空", "empty_title")
        todo["title"] = truncate_to_token_budget(t, TODO_TITLE_TOKEN_BUDGET, suffix="…")
        changed.append("title")
    if priority is not None:
        todo["priority"] = _normalize_priority(priority)
        changed.append("priority")
    if tags is not None:
        todo["tags"] = _normalize_tags(tags)
        changed.append("tags")
    if note is not None:
        todo["note"] = truncate_to_token_budget((note or "").strip(), TODO_NOTE_TOKEN_BUDGET, suffix="…")
        changed.append("note")
    if due_at is not None:
        todo["due_at"] = _normalize_due_at(due_at)
        changed.append("due_at")
    return store, {
        "ok": True,
        "action": "edit",
        "todo": _todo_summary(todo),
        "changed": changed,
        "total": len(store["todos"]),
        "pending": sum(1 for t in store["todos"] if not t["done"]),
    }


def _todo_summary(t: dict) -> dict:
    """精简的待办摘要，用于 AI 上下文与回调渲染。"""
    return {
        "id": t.get("id"),
        "title": t.get("title", ""),
        "done": bool(t.get("done")),
        "priority": t.get("priority", "medium"),
        "tags": list(t.get("tags", [])),
        "note": t.get("note", ""),
        "due_at": t.get("due_at"),
        "due_status": _due_status(t.get("due_at")),
        "created_at": t.get("created_at"),
        "completed_at": t.get("completed_at"),
    }


# ---------- 工具入口 ----------
async def execute_todo(
    chat_id: int,
    action: str = "list",
    title: Optional[str] = None,
    todo_id: Optional[str] = None,
    priority: Optional[str] = None,
    tags: Any = None,
    note: Optional[str] = None,
    due_at: Optional[str] = None,
    filter: Optional[str] = None,
    tag: Optional[str] = None,
) -> str:
    """
    todo 工具的主入口。返回 JSON 字符串供 AI 阅读，渲染层另做。

    action: add | list | done | undone | delete | clear | edit
    """
    action = (action or "list").strip().lower()
    filter_ = (filter or "all").strip().lower()
    if filter_ not in VALID_FILTERS:
        filter_ = "all"

    # TIMER 主动巡检依赖 Todo 作为第一检查项；这里记录操作元数据，
    # 不记录任务标题/备注，避免后台日志泄露不必要的用户内容。
    logger.info(
        "[TIMER-TODO] chat=%s action=%s filter=%s todo_id=%s",
        chat_id, action, filter_, todo_id or "-",
    )

    if action == "add":
        payload = await _mutate(chat_id, lambda s: _op_add(s, title, priority or "medium", tags, note, due_at))
    elif action == "list":
        payload = await _read_store(chat_id, lambda s: _op_list(s, filter_, tag, priority))
    elif action == "done":
        payload = await _mutate(chat_id, lambda s: _op_toggle(s, todo_id, True))
    elif action == "undone":
        payload = await _mutate(chat_id, lambda s: _op_toggle(s, todo_id, False))
    elif action == "toggle":
        payload = await _mutate(chat_id, lambda s: _op_toggle(s, todo_id, None))
    elif action == "delete":
        payload = await _mutate(chat_id, lambda s: _op_delete(s, todo_id))
    elif action == "clear":
        # 默认只清已完成，避免误删
        f = filter_ if filter_ in ("done", "all") else "done"
        payload = await _mutate(chat_id, lambda s: _op_clear(s, f))
    elif action == "edit":
        payload = await _mutate(chat_id, lambda s: _op_edit(s, todo_id, title, priority, tags, note, due_at))
    else:
        payload = {"ok": False, "error": f"未知 action: {action}", "code": "bad_action"}

    if isinstance(payload, dict):
        logger.info(
            "[TIMER-TODO] chat=%s result ok=%s total=%s pending=%s",
            chat_id, payload.get("ok"), payload.get("total", "-"), payload.get("pending", "-"),
        )
    return json.dumps(payload, ensure_ascii=False)


# ---------- 富文本渲染 ----------
def _esc(text: Any) -> str:
    """HTML 转义，保证 Telegram 富文本发送安全。"""
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_todo_card(payload: dict, max_items: int = 50) -> str:
    """
    将 execute_todo 的 payload 渲染为 Telegram 富文本 HTML 卡片。
    适用于 sendRichMessage（支持 <ul>/<ol>/<table>/<details> 等扩展标签）。
    """
    if not isinstance(payload, dict):
        return f"<p>{_esc(payload)}</p>"

    if not payload.get("ok"):
        return f"<p>❌ <b>待办操作失败</b></p><p>{_esc(payload.get('error', '未知错误'))}</p>"

    action = payload.get("action", "list")

    # 非列表动作给一个简洁的确认卡片
    if action == "add":
        t = payload.get("todo", {})
        return (
            f"<p>➕ <b>已新增待办</b></p>"
            f"<p>{_priority_badge(t)} {_esc(t.get('title'))} {_tag_chips(t)}</p>"
            f"<p><i>当前共 {payload.get('total', 0)} 项，待办 {payload.get('pending', 0)} 项</i></p>"
        )
    if action in ("done", "undone", "toggle"):
        t = payload.get("todo", {})
        icon = "✅" if t.get("done") else "↩️"
        verb = "标记为已完成" if t.get("done") else "标记为未完成"
        if not payload.get("changed", True):
            verb = f"状态未变化（仍为{'已完成' if t.get('done') else '未完成'}）"
        return (
            f"<p>{icon} <b>{verb}</b></p>"
            f"<p>{_priority_badge(t)} {_esc(t.get('title'))} {_tag_chips(t)}</p>"
            f"<p><i>剩余 {payload.get('pending', 0)} / {payload.get('total', 0)} 项未完成</i></p>"
        )
    if action == "delete":
        t = payload.get("todo", {})
        return (
            f"<p>🗑️ <b>已删除</b></p>"
            f"<p><s>{_priority_badge(t)} {_esc(t.get('title'))}</s> {_tag_chips(t)}</p>"
            f"<p><i>剩余 {payload.get('total', 0)} 项</i></p>"
        )
    if action == "clear":
        return (
            f"<p>🧹 <b>{_esc(payload.get('message', '已清空'))}</b></p>"
            f"<p><i>剩余 {payload.get('total', 0)} 项，未完成 {payload.get('pending', 0)} 项</i></p>"
        )
    if action == "edit":
        t = payload.get("todo", {})
        return (
            f"<p>📝 <b>已编辑</b></p>"
            f"<p>{_priority_badge(t)} {_esc(t.get('title'))} {_tag_chips(t)}</p>"
            f"<p><i>修改字段：{', '.join(payload.get('changed', [])) or '无'}</i></p>"
        )

    # ---- list 渲染 ----
    todos = payload.get("todos", []) or []
    total = payload.get("total", 0)
    done = payload.get("done", 0)
    pending = payload.get("pending", 0)
    filter_ = payload.get("filter", "all")
    tag_filter = payload.get("tag")
    prio_filter = payload.get("priority")

    header = "<h3>📋 待办清单</h3>"
    stat_line = (
        f"共 <b>{total}</b> 项 · "
        f"已完成 <b>{done}</b> · "
        f"待办 <b>{pending}</b>"
    )
    filter_desc = {
        "all": "全部",
        "pending": "仅未完成",
        "done": "仅已完成",
    }.get(filter_, "全部")
    extra = []
    if tag_filter:
        extra.append(f"标签=<code>{_esc(tag_filter)}</code>")
    if prio_filter:
        extra.append(f"优先级={PRIORITY_META.get(prio_filter, {}).get('emoji', '⚫')}{_esc(prio_filter)}")
    extra_line = f"筛选：<i>{filter_desc}</i>"
    if extra:
        extra_line += " · " + " · ".join(extra)

    if not todos:
        body = (
            f"<p>{stat_line}</p>"
            f"<p>{extra_line}</p>"
            f"<blockquote>🎉 当前筛选下没有待办项</blockquote>"
        )
        return header + body

    items_html = []
    shown = 0
    for t in todos[:max_items]:
        shown += 1
        items_html.append(_render_todo_item(t))
    extra_count = len(todos) - shown

    list_html = "<ol>" + "".join(items_html) + "</ol>"
    if extra_count > 0:
        list_html += f"<p><i>… 还有 {extra_count} 项未显示，可用更细的 filter / tag 查看</i></p>"

    return (
        header
        + f"<p>{stat_line}</p>"
        + f"<p>{extra_line}</p>"
        + "<hr/>"
        + list_html
    )


def _priority_badge(t: dict) -> str:
    p = t.get("priority", "medium")
    meta = PRIORITY_META.get(p, PRIORITY_META["medium"])
    return f"<b>{meta['emoji']} {meta['label']}</b>"


def _tag_chips(t: dict) -> str:
    tags = t.get("tags", []) or []
    if not tags:
        return ""
    return " ".join(f"<code>#{_esc(tag)}</code>" for tag in tags[:MAX_TAGS])


def _render_todo_item(t: dict) -> str:
    """单个待办的 <li>。"""
    badge = _priority_badge(t)
    title = _esc(t.get("title", ""))
    if t.get("done"):
        # 已完成：删除线 + 淡化
        title_html = f"<s>{title}</s>"
        status = "✅"
    else:
        title_html = f"<b>{title}</b>"
        status = "⬜"
    tags = _tag_chips(t)
    note = t.get("note", "")
    note_html = f"<blockquote>{_esc(note)}</blockquote>" if note else ""

    parts = [f"{status} {badge} {title_html}"]
    if tags:
        parts.append(tags)
    line = " ".join(parts)
    if note_html:
        line += note_html
    return f"<li>{line}</li>"


# ---------- 工具定义（OpenAI function-calling schema） ----------
# 注意：description 字段是给 AI 阅读的「工具说明书」。
# 全部用纯文本，不使用 Markdown 语法（如 **bold**、`code`、# 标题等），
# 与系统提示词风格保持一致——AI 输出时也不会把这些符号带进回复。
TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": (
            "Persistent per-chat todo list. 8 actions: add / list / done / undone / toggle / delete / clear / edit. "
            "Todos are stored in the dedicated state store and survive across sessions. due_at is optional and enables overdue / due-soon detection. After any write action (add/done/undone/delete/edit/clear) immediately call list so the user sees the updated state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "_description": {
                    "type": "string",
                    "description": "简述本次操作目的（≤60字）。示例：添加一条买菜的待办"
                },
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "done", "undone", "toggle", "delete", "clear", "edit"],
                    "description": "要执行的操作。默认 list。"
                },
                "title": {
                    "type": "string",
                    "description": "待办标题。add 必填，edit 可选。最多 200 tokens。"
                },
                "todo_id": {
                    "type": "string",
                    "description": "目标待办 id：8 位 hex 字符串。done/undone/toggle/delete/edit 必填。"
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "优先级，默认 medium。add/edit/list(filter) 使用。"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选标签，最多 8 个，每个最多 24 tokens。也接受逗号/空格分隔的字符串。"
                },
                "note": {
                    "type": "string",
                    "description": "可选的较长备注（最多 500 tokens）。add/edit 使用。"
                },
                "due_at": {
                    "type": "string",
                    "description": "可选截止时间，ISO 8601，例如 2026-08-29T18:00:00-07:00。add/edit 使用；list 返回 due_status=overdue/due_soon/upcoming/none。"
                },
                "filter": {
                    "type": "string",
                    "enum": ["all", "pending", "done"],
                    "description": "list/clear 的过滤条件。clear 时 done（默认）仅清除已完成，all 清空全部。"
                },
                "tag": {
                    "type": "string",
                    "description": "按标签过滤（仅 list）。"
                }
            },
            "required": ["action"]
        },
        "input_examples": [
            {"action": "add", "title": "买牛奶", "priority": "high", "tags": ["购物", "周末"], "due_at": "2026-08-29T18:00:00-07:00"},
            {"action": "list", "filter": "pending"},
            {"action": "done", "todo_id": "a1b2c3d4"},
            {"action": "delete", "todo_id": "a1b2c3d4"},
            {"action": "edit", "todo_id": "e5f6a7b8", "title": "改后的标题", "priority": "low"},
            {"action": "clear", "filter": "done"}
        ]
    }
}
