# memory_tool.py
"""长期记忆工具。"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from workspace_paths import memory_state_file
from token_budget import truncate_to_token_budget
from typing import Any, Optional

from workspace_utils import _get_workspace_lock
from state_r2 import sync_named_file_from_r2, sync_named_file_to_r2

logger = logging.getLogger(__name__)

# 常量
MEMORY_FILENAME = "memories.json"
VALID_IMPORTANCE = ("low", "medium", "high")
MEMORY_CONTENT_TOKEN_BUDGET = 2_000
MEMORY_TAG_TOKEN_BUDGET = 24
MEMORY_CARD_CONTENT_TOKEN_BUDGET = 200
MAX_TAGS = 8
MAX_MEMORIES = 1000  # 单 chat 上限，防止失控增长
MAX_BATCH_ITEMS = 100

IMPORTANCE_META = {
    "high":   {"emoji": "🔴", "label": "高"},
    "medium": {"emoji": "🟡", "label": "中"},
    "low":    {"emoji": "🟢", "label": "低"},
}

CATEGORY_EMOJI = {
    "fact":        "📌",
    "preference":  "⚙️",
    "person":      "👤",
    "event":       "📅",
    "note":        "📝",
}

# 存储层
def _memory_path(chat_id: int) -> Path:
    return memory_state_file(chat_id)

def _new_id() -> str:
    return uuid.uuid4().hex[:8]

def _empty_store() -> dict:
    return {"memories": [], "updated_at": 0}

def _load_local(chat_id: int) -> dict:
    path = _memory_path(chat_id)
    if not path.is_file():
        return _empty_store()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
            return _empty_store()
        data.setdefault("updated_at", 0)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"memories.json 读取失败 (chat={chat_id}): {e}")
        return _empty_store()

def _save_local(chat_id: int, store: dict) -> None:
    """以原子方式把 store 写到 memories.json。

    修复：之前 tmp 文件名固定为 ``memories.json.tmp``，两个并发 writer
    会复用同一个 tmp 路径，后写的覆盖先写的，导致数据丢失。现在在
    tmp 名中加入 PID + 随机后缀，确保唯一。
    """
    path = _memory_path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = int(time.time())
    # 唯一 tmp 名：进程内 PID + 8 字节随机，避免并发写碰撞。
    tmp = path.with_suffix(f".json.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)

def _find_memory(memories: list, mid: Optional[str]) -> tuple[int, dict] | None:
    if not mid:
        return None
    target = str(mid).lstrip("#")
    for i, m in enumerate(memories):
        if str(m.get("id", "")).lstrip("#") == target:
            return i, m
    return None

def _normalize_importance(value: Optional[str]) -> str:
    if not value:
        return "medium"
    v = str(value).strip().lower()
    if v in VALID_IMPORTANCE:
        return v
    alias = {"p0": "high", "p1": "high", "p2": "medium", "p3": "low",
             "高": "high", "中": "medium", "低": "low"}
    return alias.get(v, "medium")

def _normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        parts = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    elif isinstance(tags, list):
        parts = [str(t).strip() for t in tags if str(t).strip()]
    else:
        return []
    seen = set()
    out = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(truncate_to_token_budget(p, MEMORY_TAG_TOKEN_BUDGET, suffix="…"))
        if len(out) >= MAX_TAGS:
            break
    return out

def _normalize_category(value: Optional[str]) -> str:
    if not value:
        return "note"
    v = truncate_to_token_budget(str(value).strip().lower(), 32, suffix="…")
    return v or "note"

# 业务逻辑
class _MemoryError(Exception):
    def __init__(self, message: str, code: str = "memory_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

async def _read_store(chat_id: int, fn: Callable[[dict], tuple[dict, dict]]) -> dict:
    """
    读取型操作：先从 R2 拉取最新内容到本地，再执行读取，不回写 store。
    """
    lock = await _get_workspace_lock(chat_id)
    async with lock:
        try:
            await sync_named_file_from_r2(chat_id, _memory_path(chat_id), MEMORY_FILENAME)
        except Exception as e:
            logger.warning(f"memory: R2→local 同步失败 (chat={chat_id}): {e}")
        store = _load_local(chat_id)
        try:
            _store, payload = fn(store)
        except _MemoryError as e:
            return {"ok": False, "error": str(e), "code": e.code}
        return payload

async def _mutate(chat_id: int, fn: Callable[[dict], tuple[dict, dict]]) -> dict:
    """
    写入型操作：R2 → 本地 → 修改 → 保存 → 回传 R2。
    """
    lock = await _get_workspace_lock(chat_id)
    async with lock:
        try:
            await sync_named_file_from_r2(chat_id, _memory_path(chat_id), MEMORY_FILENAME)
        except Exception as e:
            logger.warning(f"memory: R2→local 同步失败 (chat={chat_id}): {e}")
        store = _load_local(chat_id)
        try:
            store, payload = fn(store)
        except _MemoryError as e:
            return {"ok": False, "error": str(e), "code": e.code}
        _save_local(chat_id, store)
        try:
            await sync_named_file_to_r2(chat_id, _memory_path(chat_id), MEMORY_FILENAME)
        except Exception as e:
            logger.warning(f"memory: local→R2 同步失败 (chat={chat_id}): {e}")
        return payload

def _op_add(store: dict, content: Optional[str], category: str, tags: list[str],
            importance: str, source: str) -> tuple[dict, dict]:
    content = (content or "").strip()
    if not content:
        raise _MemoryError("content 不能为空", "empty_content")
    content = truncate_to_token_budget(content, MEMORY_CONTENT_TOKEN_BUDGET, suffix="…")
    if len(store["memories"]) >= MAX_MEMORIES:
        raise _MemoryError(f"记忆数量已达上限 {MAX_MEMORIES}，请先清理", "too_many")

    now = int(time.time())
    mem = {
        "id": _new_id(),
        "content": content,
        "category": _normalize_category(category),
        "tags": _normalize_tags(tags),
        "importance": _normalize_importance(importance),
        "created_at": now,
        "updated_at": now,
        "source": truncate_to_token_budget((source or "agent").strip().lower(), 16, suffix="…") or "agent",
    }
    store["memories"].append(mem)
    return store, {
        "ok": True,
        "action": "add",
        "memory": _mem_summary(mem),
        "total": len(store["memories"]),
    }

def _op_get(store: dict, mid: Optional[str]) -> tuple[dict, dict]:
    found = _find_memory(store["memories"], mid)
    if not found:
        raise _MemoryError(f"找不到 id 为 {mid} 的记忆", "not_found")
    _, mem = found
    return store, {"ok": True, "action": "get", "memory": _mem_summary(mem),
                   "total": len(store["memories"])}

def _op_list(store: dict, category: Optional[str], tag: Any,
             importance: Optional[str], limit: int) -> tuple[dict, dict]:
    memories = store["memories"]
    # 默认排序：重要性降序，再按创建时间倒序（新的在前）
    weight = {"high": 3, "medium": 2, "low": 1}
    memories_sorted = sorted(
        memories,
        key=lambda m: (
            -weight.get(m.get("importance", "medium"), 2),
            -int(m.get("created_at", 0) or 0),
            str(m.get("id", "")),
        ),
    )

    filtered = []
    cat_filter = _normalize_category(category) if category else None
    imp_filter = _normalize_importance(importance) if importance else None
    tag_filters = _normalize_tags(tag)
    for m in memories_sorted:
        if cat_filter and m.get("category") != cat_filter:
            continue
        if imp_filter and m.get("importance") != imp_filter:
            continue
        if tag_filters and not any(t in m.get("tags", []) for t in tag_filters):
            continue
        filtered.append(m)
        if limit and len(filtered) >= limit:
            break

    return store, {
        "ok": True,
        "action": "list",
        "category": category,
        "tag": tag if isinstance(tag, str) else None,
        "tags": tag_filters or None,
        "importance": importance,
        "memories": [_mem_summary(m) for m in filtered],
        "total": len(memories),
        "shown": len(filtered),
        "result_count": len(filtered),
    }

def _op_search(store: dict, query: str, limit: int) -> tuple[dict, dict]:
    q = (query or "").strip().lower()
    if not q:
        raise _MemoryError("search query 不能为空", "empty_query")
    memories = store["memories"]
    matches = []
    for m in memories:
        haystack_parts = [m.get("content", ""),
                          m.get("category", ""),
                          " ".join(m.get("tags", []))]
        haystack = "\n".join(haystack_parts).lower()
        if q in haystack:
            matches.append(m)
    # 同样的排序
    weight = {"high": 3, "medium": 2, "low": 1}
    matches.sort(key=lambda m: (
        -weight.get(m.get("importance", "medium"), 2),
        -int(m.get("created_at", 0) or 0),
        str(m.get("id", "")),
    ))
    if limit and len(matches) > limit:
        matches = matches[:limit]

    return store, {
        "ok": True,
        "action": "search",
        "query": query,
        "matches": len(matches),
        "result_count": len(matches),
        "total": len(memories),
        "memories": [_mem_summary(m) for m in matches],
    }

def _store_stats(store: dict) -> dict:
    return {"total": len(store["memories"])}


def _normalize_id_list(values: Any, field_name: str = "ids") -> list[str]:
    if not isinstance(values, list) or not values:
        raise _MemoryError(f"{field_name} 必须是非空数组", "invalid_ids")
    if len(values) > MAX_BATCH_ITEMS:
        raise _MemoryError(f"{field_name} 最多 {MAX_BATCH_ITEMS} 项", "batch_too_large")
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        value_s = str(value or "").strip().lstrip("#")
        if not value_s:
            raise _MemoryError(f"{field_name} 不能包含空 id", "invalid_ids")
        if value_s not in seen:
            seen.add(value_s)
            out.append(value_s)
    if not out:
        raise _MemoryError(f"{field_name} 不能为空", "invalid_ids")
    return out


def _op_update(store: dict, mid: Optional[str], content: Optional[str],
               category: Optional[str], tags: Any, importance: Optional[str]) -> tuple[dict, dict]:
    found = _find_memory(store["memories"], mid)
    if not found:
        raise _MemoryError(f"找不到 id 为 {mid} 的记忆", "not_found")
    _, mem = found
    changed = []
    if content is not None:
        c = content.strip()
        if not c:
            raise _MemoryError("content 不能为空", "empty_content")
        mem["content"] = truncate_to_token_budget(c, MEMORY_CONTENT_TOKEN_BUDGET, suffix="…")
        changed.append("content")
    if category is not None:
        mem["category"] = _normalize_category(category)
        changed.append("category")
    if tags is not None:
        mem["tags"] = _normalize_tags(tags)
        changed.append("tags")
    if importance is not None:
        mem["importance"] = _normalize_importance(importance)
        changed.append("importance")
    if changed:
        mem["updated_at"] = int(time.time())
    return store, {
        "ok": True,
        "action": "update",
        "memory": _mem_summary(mem),
        "changed": changed,
        "affected_count": 1,
        "changed_count": 1 if changed else 0,
        **_store_stats(store),
    }


def _op_delete(store: dict, mid: Optional[str]) -> tuple[dict, dict]:
    found = _find_memory(store["memories"], mid)
    if not found:
        raise _MemoryError(f"找不到 id 为 {mid} 的记忆", "not_found")
    idx, mem = found
    store["memories"].pop(idx)
    return store, {
        "ok": True,
        "action": "delete",
        "memory": _mem_summary(mem),
        "affected_count": 1,
        **_store_stats(store),
    }


def _op_add_many(store: dict, items: Any) -> tuple[dict, dict]:
    if not isinstance(items, list) or not items:
        raise _MemoryError("memories 必须是非空数组", "invalid_items")
    if len(items) > MAX_BATCH_ITEMS:
        raise _MemoryError(f"memories 最多 {MAX_BATCH_ITEMS} 项", "batch_too_large")
    added: list[dict] = []
    failed: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            failed.append({"index": index, "code": "invalid_item", "error": "每个 memory 必须是对象"})
            continue
        try:
            _store, payload = _op_add(
                store,
                item.get("content"),
                item.get("category") or "note",
                item.get("tags"),
                item.get("importance") or "medium",
                item.get("source") or "agent",
            )
            store = _store
            added.append(payload["memory"])
        except _MemoryError as exc:
            failed.append({"index": index, "code": exc.code, "error": str(exc)})
    return store, {
        "ok": True,
        "action": "add_many",
        "requested_count": len(items),
        "affected_count": len(added),
        "memories": added,
        "failed": failed,
        **_store_stats(store),
    }


def _op_get_many(store: dict, memory_ids: Any) -> tuple[dict, dict]:
    ids = _normalize_id_list(memory_ids, "memory_ids")
    found_items: list[dict] = []
    failed: list[dict] = []
    for mid in ids:
        found = _find_memory(store["memories"], mid)
        if not found:
            failed.append({"id": mid, "code": "not_found", "error": f"找不到 id 为 {mid} 的记忆"})
            continue
        _idx, mem = found
        found_items.append(_mem_summary(mem))
    return store, {
        "ok": True,
        "action": "get_many",
        "requested_count": len(ids),
        "found_count": len(found_items),
        "result_count": len(found_items),
        "memories": found_items,
        "failed": failed,
        **_store_stats(store),
    }


def _op_update_many(store: dict, updates: Any) -> tuple[dict, dict]:
    if not isinstance(updates, list) or not updates:
        raise _MemoryError("updates 必须是非空数组", "invalid_items")
    if len(updates) > MAX_BATCH_ITEMS:
        raise _MemoryError(f"updates 最多 {MAX_BATCH_ITEMS} 项", "batch_too_large")
    updated: list[dict] = []
    failed: list[dict] = []
    for index, item in enumerate(updates):
        if not isinstance(item, dict):
            failed.append({"index": index, "code": "invalid_item", "error": "每个 update 必须是对象"})
            continue
        mid = item.get("memory_id")
        try:
            _store, payload = _op_update(
                store,
                mid,
                item.get("content"),
                item.get("category"),
                item.get("tags"),
                item.get("importance"),
            )
            store = _store
            updated.append({"changed": payload.get("changed", []), "memory": payload["memory"]})
        except _MemoryError as exc:
            failed.append({"id": str(mid or ""), "code": exc.code, "error": str(exc)})
    return store, {
        "ok": True,
        "action": "update_many",
        "requested_count": len(updates),
        "affected_count": len(updated),
        "changed_count": sum(1 for item in updated if item.get("changed")),
        "updated": updated,
        "failed": failed,
        **_store_stats(store),
    }


def _op_delete_many(store: dict, memory_ids: Any) -> tuple[dict, dict]:
    ids = _normalize_id_list(memory_ids, "memory_ids")
    deleted: list[dict] = []
    failed: list[dict] = []
    for mid in ids:
        found = _find_memory(store["memories"], mid)
        if not found:
            failed.append({"id": mid, "code": "not_found", "error": f"找不到 id 为 {mid} 的记忆"})
            continue
        idx, mem = found
        store["memories"].pop(idx)
        deleted.append(_mem_summary(mem))
    return store, {
        "ok": True,
        "action": "delete_many",
        "requested_count": len(ids),
        "affected_count": len(deleted),
        "deleted": deleted,
        "failed": failed,
        **_store_stats(store),
    }


def _op_clear(store: dict, scope: str) -> tuple[dict, dict]:
    """scope = all / category:<name> / tag:<name>"""
    before = len(store["memories"])
    if scope == "all":
        store["memories"] = []
        removed = before
        msg = f"已清空全部 {removed} 条记忆"
    elif scope.startswith("category:"):
        cat = _normalize_category(scope.split(":", 1)[1])
        before_list = list(store["memories"])
        store["memories"] = [m for m in before_list if m.get("category") != cat]
        removed = before - len(store["memories"])
        msg = f"已清空分类 {cat} 下 {removed} 条记忆"
    elif scope.startswith("tag:"):
        tag = scope.split(":", 1)[1].strip()
        if not tag:
            raise _MemoryError("clear tag 不能为空", "bad_scope")
        before_list = list(store["memories"])
        store["memories"] = [m for m in before_list if tag not in m.get("tags", [])]
        removed = before - len(store["memories"])
        msg = f"已清空标签 #{tag} 下 {removed} 条记忆"
    else:
        raise _MemoryError(f"不支持的 clear scope: {scope}", "bad_scope")
    return store, {
        "ok": True,
        "action": "clear",
        "scope": scope,
        "removed": removed,
        "affected_count": removed,
        "message": msg,
        **_store_stats(store),
    }

def _mem_summary(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "content": m.get("content", ""),
        "category": m.get("category", "note"),
        "tags": list(m.get("tags", [])),
        "importance": m.get("importance", "medium"),
        "created_at": m.get("created_at"),
        "updated_at": m.get("updated_at"),
        "source": m.get("source", "agent"),
    }

# 工具入口
async def execute_memory(
    chat_id: int,
    action: str = "list",
    content: Optional[str] = None,
    memory_id: Optional[str] = None,
    memory_ids: Any = None,
    memories: Any = None,
    updates: Any = None,
    category: Optional[str] = None,
    tags: Any = None,
    importance: Optional[str] = None,
    query: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = 50,
    source: str = "agent",
) -> str:
    """长期记忆工具主入口。单项动作与显式 *_many 批量动作。"""
    action = (action or "list").strip().lower()
    try:
        limit_i = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        limit_i = 50

    if action == "add":
        payload = await _mutate(chat_id, lambda s: _op_add(s, content, category or "note", tags,
                                                            importance or "medium", source))
    elif action == "add_many":
        payload = await _mutate(chat_id, lambda s: _op_add_many(s, memories))
    elif action == "get":
        payload = await _read_store(chat_id, lambda s: _op_get(s, memory_id))
    elif action == "get_many":
        payload = await _read_store(chat_id, lambda s: _op_get_many(s, memory_ids))
    elif action == "list":
        payload = await _read_store(chat_id, lambda s: _op_list(s, category, tags, importance, limit_i))
    elif action == "search":
        payload = await _read_store(chat_id, lambda s: _op_search(s, query or "", limit_i))
    elif action == "update":
        payload = await _mutate(chat_id, lambda s: _op_update(s, memory_id, content, category, tags, importance))
    elif action == "update_many":
        payload = await _mutate(chat_id, lambda s: _op_update_many(s, updates))
    elif action == "delete":
        payload = await _mutate(chat_id, lambda s: _op_delete(s, memory_id))
    elif action == "delete_many":
        payload = await _mutate(chat_id, lambda s: _op_delete_many(s, memory_ids))
    elif action == "clear":
        payload = await _mutate(chat_id, lambda store: _op_clear(store, scope or "all"))
    else:
        payload = {"ok": False, "error": f"未知 action: {action}", "code": "bad_action"}

    return json.dumps(payload, ensure_ascii=False)

# 富文本渲染
# esc 统一来自 core.text_utils.escape_html_text（卡片动态片段的严格转义）。
from core.text_utils import escape_html_text as _esc

def _importance_badge(m: dict) -> str:
    p = m.get("importance", "medium")
    meta = IMPORTANCE_META.get(p, IMPORTANCE_META["medium"])
    return f"<b>{meta['emoji']} {meta['label']}</b>"

def _category_badge(m: dict) -> str:
    c = m.get("category", "note")
    emoji = CATEGORY_EMOJI.get(c, "🏷️")
    return f"<code>{emoji} {_esc(c)}</code>"

def _tag_chips(m: dict) -> str:
    tags = m.get("tags", []) or []
    if not tags:
        return ""
    return " ".join(f"<code>#{_esc(t)}</code>" for t in tags[:MAX_TAGS])

def render_memory_card(payload: dict, max_items: int = 30) -> str:
    """将 execute_memory 返回的 payload 渲染成 Telegram 富文本卡片。"""
    if not isinstance(payload, dict):
        return f"<p>{_esc(payload)}</p>"

    if not payload.get("ok"):
        return (f"<p>❌ <b>记忆操作失败</b></p>"
                f"<p>{_esc(payload.get('error', '未知错误'))}</p>")

    action = payload.get("action", "list")

    if action in ("add_many", "get_many", "update_many", "delete_many"):
        if action == "add_many":
            title = "🧠 <b>已批量保存</b>"
            items = payload.get("memories", []) or []
            count = payload.get("affected_count", 0)
        elif action == "get_many":
            title = "🔎 <b>已批量读取</b>"
            items = payload.get("memories", []) or []
            count = payload.get("found_count", 0)
        elif action == "update_many":
            title = "📝 <b>已批量更新</b>"
            items = payload.get("updated", []) or []
            count = payload.get("affected_count", 0)
        else:
            title = "🗑️ <b>已批量删除</b>"
            items = payload.get("deleted", []) or []
            count = payload.get("affected_count", 0)
        parts = [f"<p>{title} {count} 条</p>"]
        if items:
            rendered_items = []
            for item in items[:30]:
                if action == "update_many" and isinstance(item, dict):
                    item = item.get("memory", item)
                rendered_items.append(_render_memory_item(item if isinstance(item, dict) else {}))
            parts.append("<ol>" + "".join(rendered_items) + "</ol>")
            if len(items) > 30:
                parts.append(f"<p><i>… 还有 {len(items) - 30} 条</i></p>")
        failed = payload.get("failed", []) or []
        if failed:
            parts.append(f"<p>⚠️ 失败 {len(failed)} 条</p>")
        if "total" in payload:
            parts.append(f"<p><i>当前共 {payload.get('total', 0)} 条记忆</i></p>")
        return "".join(parts)

    if action == "add":
        m = payload.get("memory", {})
        return (
            f"<p>🧠 <b>已保存记忆</b> <code>#{m.get('id', '?')}</code></p>"
            f"<p>{_importance_badge(m)} {_category_badge(m)} {_tag_chips(m)}</p>"
            f"<blockquote>{_esc(m.get('content'))}</blockquote>"
        )
    if action == "get":
        m = payload.get("memory", {})
        return _render_memory_detail(m)
    if action == "update":
        m = payload.get("memory", {})
        return (
            f"<p>📝 <b>已更新记忆</b> <code>#{m.get('id', '?')}</code></p>"
            f"<p>{_importance_badge(m)} {_category_badge(m)} {_tag_chips(m)}</p>"
            f"<blockquote>{_esc(m.get('content'))}</blockquote>"
            f"<p><i>修改字段：{', '.join(payload.get('changed', [])) or '无'}</i></p>"
        )
    if action == "delete":
        m = payload.get("memory", {})
        return (
            f"<p>🗑️ <b>已删除记忆</b> <code>#{m.get('id', '?')}</code></p>"
            f"<blockquote><s>{_esc(m.get('content'))}</s></blockquote>"
        )
    if action == "clear":
        return (
            f"<p>🧹 <b>{_esc(payload.get('message', '已清空'))}</b></p>"
            f"<p><i>剩余 {payload.get('total', 0)} 条</i></p>"
        )
    if action == "search":
        q = payload.get("query", "")
        matches = payload.get("memories", []) or []
        header = f"<h3>🔎 记忆搜索：<code>{_esc(q)}</code></h3>"
        if not matches:
            return header + "<blockquote>没有匹配的记忆</blockquote>"
        items = "".join(_render_memory_item(m) for m in matches[:max_items])
        extra = len(matches) - max_items
        extra_html = (f"<p><i>… 还有 {extra} 条未显示</i></p>" if extra > 0 else "")
        return header + f"<p>命中 <b>{payload.get('matches', 0)}</b> / {payload.get('total', 0)} 条</p><hr/><ol>{items}</ol>{extra_html}"

    # list 渲染
    memories = payload.get("memories", []) or []
    total = payload.get("total", 0)
    shown = payload.get("shown", len(memories))
    header = "<h3>🧠 长期记忆库</h3>"
    stat = f"共 <b>{total}</b> 条 · 显示 <b>{shown}</b> 条"
    extra_desc = []
    if payload.get("category"):
        extra_desc.append(f"分类=<code>{_esc(payload['category'])}</code>")
    if payload.get("tags"):
        tags_value = payload["tags"]
        if isinstance(tags_value, list):
            extra_desc.append("标签=" + " ".join(f"<code>#{_esc(t)}</code>" for t in tags_value[:MAX_TAGS]))
        else:
            extra_desc.append(f"标签=<code>#{_esc(tags_value)}</code>")
    elif payload.get("tag"):
        extra_desc.append(f"标签=<code>#{_esc(payload['tag'])}</code>")
    if payload.get("importance"):
        p = payload["importance"]
        meta = IMPORTANCE_META.get(p, {})
        extra_desc.append(f"重要性={meta.get('emoji', '⚫')}{_esc(p)}")
    extra_line = f"筛选：<i>{' · '.join(extra_desc) if extra_desc else '全部'}</i>"

    if not memories:
        return header + f"<p>{stat}</p><p>{extra_line}</p><blockquote>📭 当前没有任何记忆</blockquote>"

    items = "".join(_render_memory_item(m) for m in memories[:max_items])
    extra = len(memories) - max_items
    extra_html = (f"<p><i>… 还有 {extra} 条未显示，可用 search 或更细的过滤查看</i></p>"
                  if extra > 0 else "")
    return header + f"<p>{stat}</p><p>{extra_line}</p><hr/><ol>{items}</ol>{extra_html}"

def _render_memory_item(m: dict) -> str:
    badge = _importance_badge(m)
    cat = _category_badge(m)
    mid = f"<code>#{_esc(m.get('id', '?'))}</code>"
    content = _esc(m.get("content", ""))
    content = truncate_to_token_budget(content, MEMORY_CARD_CONTENT_TOKEN_BUDGET, suffix="…")
    tags = _tag_chips(m)
    parts = [f"{badge} {cat} {mid}", f"<blockquote>{content}</blockquote>"]
    if tags:
        parts.append(tags)
    return f"<li>{' '.join(parts[:1])} {' '.join(parts[1:])}</li>"

def _render_memory_detail(m: dict) -> str:
    if not m:
        return "<p>记忆不存在</p>"
    parts = [
        f"<h3>🧠 记忆 #{_esc(m.get('id', '?'))}</h3>",
        f"<p>{_importance_badge(m)} {_category_badge(m)} {_tag_chips(m)}</p>",
        f"<blockquote>{_esc(m.get('content'))}</blockquote>",
    ]
    if m.get("created_at"):
        parts.append(f"<p><i>创建于 {m['created_at']} · 更新于 {m.get('updated_at', m['created_at'])} · 来源 {m.get('source', 'agent')}</i></p>")
    return "".join(parts)

# 工具定义（OpenAI function-calling schema）
# 注意：description 字段是给 AI 阅读的「工具说明书」，全部用纯文本，
# 不使用 Markdown 语法，与系统提示词风格保持一致。
# MEMORY_TOOL schema 已迁至 mcpserver/catalogue.py（内部 MCP 服务器单一数据源）。
