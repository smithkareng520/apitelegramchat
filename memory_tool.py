# memory_tool.py
"""
长期记忆工具。

定位
----
- 不同于对话历史（短期、会被自动修剪），memory 是用户希望长期保留的事实、
  偏好、要点——跨会话持久化。
- 按 chat 隔离，落在 ./workspace/{chat_id}/memories.json，复用既有 R2 同步链路。
- 给 AI 一组 CRUD + 检索接口：add / get / list / search / update / delete / clear。

数据模型
--------
每条 memory：
  {
    "id":        8 位短 id
    "seq":       显示用序号
    "content":   记忆正文（<=2000 字符）
    "category":  分类标签（fact / preference / person / event / note / custom...）
    "tags":      [str, ...]   可选标签
    "importance":low/medium/high
    "created_at": unix
    "updated_at": unix
    "source":    "agent" / "user"   来源（谁写入的）
  }

检索
----
- list 按 category / tag / importance 过滤
- search 用简单的子串匹配（大小写不敏感）扫 content + tags + category，
  无外部依赖、零成本、跨语言可用
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from workspace_paths import workspace_root
from typing import Any, Optional

from workspace_utils import (
    _get_workspace_lock,
    _sync_file_from_r2,
    _sync_file_to_r2,
)
from config import BASE_URL  # noqa: F401  — 保留给将来扩展（推送卡片用）

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
MEMORY_FILENAME = "memories.json"
VALID_IMPORTANCE = ("low", "medium", "high")
MAX_CONTENT_LEN = 2000
MAX_TAG_LEN = 24
MAX_TAGS = 8
MAX_MEMORIES = 1000  # 单 chat 上限，防止失控增长
DEFAULT_CATEGORIES = ("fact", "preference", "person", "event", "note")

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


# ---------- 存储层 ----------
def _workspace_path(chat_id: int) -> Path:
    return workspace_root(chat_id)


def _memory_path(chat_id: int) -> Path:
    return _workspace_path(chat_id) / MEMORY_FILENAME


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _empty_store() -> dict:
    return {"memories": [], "next_seq": 1, "updated_at": 0}


def _load_local(chat_id: int) -> dict:
    path = _memory_path(chat_id)
    if not path.is_file():
        return _empty_store()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
            return _empty_store()
        data.setdefault("next_seq", 1)
        data.setdefault("updated_at", 0)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"memories.json 读取失败 (chat={chat_id}): {e}")
        return _empty_store()


def _save_local(chat_id: int, store: dict) -> None:
    path = _memory_path(chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = int(time.time())
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _find_memory(memories: list, mid: str) -> tuple[int, dict] | None:
    if not mid:
        return None
    target = str(mid).lstrip("#")
    for i, m in enumerate(memories):
        if m.get("id") == mid or str(m.get("seq", "")) == target:
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
            out.append(p[:MAX_TAG_LEN])
        if len(out) >= MAX_TAGS:
            break
    return out


def _normalize_category(value: Optional[str]) -> str:
    if not value:
        return "note"
    v = str(value).strip().lower()[:32]
    return v or "note"


# ---------- 业务逻辑 ----------
class _MemoryError(Exception):
    def __init__(self, message: str, code: str = "memory_error"):
        super().__init__(message)
        self.message = message
        self.code = code


async def _mutate(chat_id: int, fn) -> dict:
    """
    性能优化：只同步 memories.json 单个文件（而非全量 workspace）。
    """
    lock = await _get_workspace_lock(chat_id)
    async with lock:
        try:
            await _sync_file_from_r2(chat_id, MEMORY_FILENAME)
        except Exception as e:
            logger.warning(f"memory: R2→local 同步失败 (chat={chat_id}): {e}")
        store = _load_local(chat_id)
        try:
            store, payload = fn(store)
        except _MemoryError as e:
            return {"ok": False, "error": str(e), "code": e.code}
        _save_local(chat_id, store)
        try:
            await _sync_file_to_r2(chat_id, MEMORY_FILENAME)
        except Exception as e:
            logger.warning(f"memory: local→R2 同步失败 (chat={chat_id}): {e}")
        return payload


def _op_add(store: dict, content: str, category: str, tags: list[str],
            importance: str, source: str) -> dict:
    content = (content or "").strip()
    if not content:
        raise _MemoryError("content 不能为空", "empty_content")
    if len(content) > MAX_CONTENT_LEN:
        content = content[:MAX_CONTENT_LEN]
    if len(store["memories"]) >= MAX_MEMORIES:
        raise _MemoryError(f"记忆数量已达上限 {MAX_MEMORIES}，请先清理", "too_many")

    seq = store.get("next_seq", 1)
    now = int(time.time())
    mem = {
        "id": _new_id(),
        "seq": seq,
        "content": content,
        "category": _normalize_category(category),
        "tags": _normalize_tags(tags),
        "importance": _normalize_importance(importance),
        "created_at": now,
        "updated_at": now,
        "source": (source or "agent").strip().lower()[:16] or "agent",
    }
    store["memories"].append(mem)
    store["next_seq"] = seq + 1
    return store, {
        "ok": True,
        "action": "add",
        "memory": _mem_summary(mem),
        "total": len(store["memories"]),
    }


def _op_get(store: dict, mid: str) -> dict:
    found = _find_memory(store["memories"], mid)
    if not found:
        raise _MemoryError(f"找不到 id 为 {mid} 的记忆", "not_found")
    _, mem = found
    return store, {"ok": True, "action": "get", "memory": _mem_summary(mem),
                   "total": len(store["memories"])}


def _op_list(store: dict, category: Optional[str], tag: Optional[str],
             importance: Optional[str], limit: int) -> dict:
    memories = store["memories"]
    # 默认排序：重要性降序，再按 seq 倒序（新的在前）
    weight = {"high": 3, "medium": 2, "low": 1}
    memories_sorted = sorted(
        memories,
        key=lambda m: (-weight.get(m.get("importance", "medium"), 2),
                       -m.get("seq", 0)),
    )

    filtered = []
    cat_filter = _normalize_category(category) if category else None
    imp_filter = _normalize_importance(importance) if importance else None
    for m in memories_sorted:
        if cat_filter and m.get("category") != cat_filter:
            continue
        if imp_filter and m.get("importance") != imp_filter:
            continue
        if tag and tag not in m.get("tags", []):
            continue
        filtered.append(m)
        if limit and len(filtered) >= limit:
            break

    return store, {
        "ok": True,
        "action": "list",
        "category": category,
        "tag": tag,
        "importance": importance,
        "memories": [_mem_summary(m) for m in filtered],
        "total": len(memories),
        "shown": len(filtered),
    }


def _op_search(store: dict, query: str, limit: int) -> dict:
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
    matches.sort(key=lambda m: (-weight.get(m.get("importance", "medium"), 2),
                                -m.get("seq", 0)))
    if limit and len(matches) > limit:
        matches = matches[:limit]

    return store, {
        "ok": True,
        "action": "search",
        "query": query,
        "matches": len(matches),
        "total": len(memories),
        "memories": [_mem_summary(m) for m in matches],
    }


def _op_update(store: dict, mid: str, content: Optional[str],
               category: Optional[str], tags: Any, importance: Optional[str]) -> dict:
    found = _find_memory(store["memories"], mid)
    if not found:
        raise _MemoryError(f"找不到 id 为 {mid} 的记忆", "not_found")
    _, mem = found
    changed = []
    if content is not None:
        c = content.strip()
        if not c:
            raise _MemoryError("content 不能为空", "empty_content")
        mem["content"] = c[:MAX_CONTENT_LEN]
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
        "total": len(store["memories"]),
    }


def _op_delete(store: dict, mid: str) -> dict:
    found = _find_memory(store["memories"], mid)
    if not found:
        raise _MemoryError(f"找不到 id 为 {mid} 的记忆", "not_found")
    idx, mem = found
    store["memories"].pop(idx)
    return store, {
        "ok": True,
        "action": "delete",
        "memory": _mem_summary(mem),
        "total": len(store["memories"]),
    }


def _op_clear(store: dict, scope: str) -> dict:
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
        "message": msg,
        "total": len(store["memories"]),
    }


def _mem_summary(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "seq": m.get("seq"),
        "content": m.get("content", ""),
        "category": m.get("category", "note"),
        "tags": list(m.get("tags", [])),
        "importance": m.get("importance", "medium"),
        "created_at": m.get("created_at"),
        "updated_at": m.get("updated_at"),
        "source": m.get("source", "agent"),
    }


# ---------- 工具入口 ----------
async def execute_memory(
    chat_id: int,
    action: str = "list",
    content: Optional[str] = None,
    memory_id: Optional[str] = None,
    category: Optional[str] = None,
    tags: Any = None,
    importance: Optional[str] = None,
    query: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = 50,
    source: str = "agent",
) -> str:
    """
    memory 工具主入口。返回 JSON 字符串。

    action: add | get | list | search | update | delete | clear
    """
    action = (action or "list").strip().lower()
    try:
        limit_i = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        limit_i = 50

    if action == "add":
        return json.dumps(
            await _mutate(chat_id, lambda s: _op_add(s, content, category or "note", tags,
                                                      importance or "medium", source)),
            ensure_ascii=False,
        )
    if action == "get":
        return json.dumps(
            await _mutate(chat_id, lambda s: _op_get(s, memory_id)),
            ensure_ascii=False,
        )
    if action == "list":
        return json.dumps(
            await _mutate(chat_id, lambda s: _op_list(s, category, tags, importance, limit_i)),
            ensure_ascii=False,
        )
    if action == "search":
        return json.dumps(
            await _mutate(chat_id, lambda s: _op_search(s, query or "", limit_i)),
            ensure_ascii=False,
        )
    if action == "update":
        return json.dumps(
            await _mutate(chat_id, lambda s: _op_update(s, memory_id, content, category, tags, importance)),
            ensure_ascii=False,
        )
    if action == "delete":
        return json.dumps(
            await _mutate(chat_id, lambda s: _op_delete(s, memory_id)),
            ensure_ascii=False,
        )
    if action == "clear":
        s = scope or "all"
        return json.dumps(
            await _mutate(chat_id, lambda store: _op_clear(store, s)),
            ensure_ascii=False,
        )

    return json.dumps({"ok": False, "error": f"未知 action: {action}", "code": "bad_action"},
                      ensure_ascii=False)


# ---------- 富文本渲染 ----------
def _esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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

    if action == "add":
        m = payload.get("memory", {})
        return (
            f"<p>🧠 <b>已保存记忆</b> <code>#{m.get('seq')}</code></p>"
            f"<p>{_importance_badge(m)} {_category_badge(m)} {_tag_chips(m)}</p>"
            f"<blockquote>{_esc(m.get('content'))}</blockquote>"
        )
    if action == "get":
        m = payload.get("memory", {})
        return _render_memory_detail(m)
    if action == "update":
        m = payload.get("memory", {})
        return (
            f"<p>📝 <b>已更新记忆</b> <code>#{m.get('seq')}</code></p>"
            f"<p>{_importance_badge(m)} {_category_badge(m)} {_tag_chips(m)}</p>"
            f"<blockquote>{_esc(m.get('content'))}</blockquote>"
            f"<p><i>修改字段：{', '.join(payload.get('changed', [])) or '无'}</i></p>"
        )
    if action == "delete":
        m = payload.get("memory", {})
        return (
            f"<p>🗑️ <b>已删除记忆</b> <code>#{m.get('seq')}</code></p>"
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

    # ---- list 渲染 ----
    memories = payload.get("memories", []) or []
    total = payload.get("total", 0)
    shown = payload.get("shown", len(memories))
    header = "<h3>🧠 长期记忆库</h3>"
    stat = f"共 <b>{total}</b> 条 · 显示 <b>{shown}</b> 条"
    extra_desc = []
    if payload.get("category"):
        extra_desc.append(f"分类=<code>{_esc(payload['category'])}</code>")
    if payload.get("tag"):
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
    seq = f"<code>#{m.get('seq', '?')}</code>"
    content = _esc(m.get("content", ""))
    # 内容超过 ~200 字截断
    if len(content) > 400:
        content = content[:400] + "…"
    tags = _tag_chips(m)
    parts = [f"{badge} {cat} {seq}", f"<blockquote>{content}</blockquote>"]
    if tags:
        parts.append(tags)
    return f"<li>{' '.join(parts[:1])} {' '.join(parts[1:])}</li>"


def _render_memory_detail(m: dict) -> str:
    if not m:
        return "<p>记忆不存在</p>"
    parts = [
        f"<h3>🧠 记忆 #{m.get('seq', '?')}</h3>",
        f"<p>{_importance_badge(m)} {_category_badge(m)} {_tag_chips(m)}</p>",
        f"<blockquote>{_esc(m.get('content'))}</blockquote>",
    ]
    if m.get("created_at"):
        parts.append(f"<p><i>创建于 {m['created_at']} · 更新于 {m.get('updated_at', m['created_at'])} · 来源 {m.get('source', 'agent')}</i></p>")
    return "".join(parts)


# ---------- 工具定义（OpenAI function-calling schema） ----------
# 注意：description 字段是给 AI 阅读的「工具说明书」，全部用纯文本，
# 不使用 Markdown 语法，与系统提示词风格保持一致。
MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Persistent per-chat long-term memory store. Use to remember facts, preferences, people, events, or any note that should survive across sessions (unlike short-lived conversation history). 7 actions: add / get / list / search / update / delete / clear. Each memory carries category, tags, and importance (low/medium/high). Write when the user mentions something worth remembering; search before answering preference-related questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "_description": {
                    "type": "string",
                    "description": "简述本次操作目的（≤60字）。示例：保存用户偏好：喜欢清淡口味"
                },
                "action": {
                    "type": "string",
                    "enum": ["add", "get", "list", "search", "update", "delete", "clear"],
                    "description": "要执行的操作。默认 list。"
                },
                "content": {
                    "type": "string",
                    "description": "记忆内容。add/update 必填。最长 2000 字符。"
                },
                "memory_id": {
                    "type": "string",
                    "description": "目标记忆 id：8 位 hex 或显示序号（带/不带 # 前缀均可）。get/update/delete 必填。"
                },
                "category": {
                    "type": "string",
                    "description": "记忆分类。内置：fact / preference / person / event / note；也可用自定义字符串。"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选标签，最多 8 个，每个 ≤24 字符。也接受逗号/空格分隔的字符串。"
                },
                "importance": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "重要程度，默认 medium。"
                },
                "query": {
                    "type": "string",
                    "description": "搜索查询（仅 search）。对 content+tags+category 做子串匹配。"
                },
                "scope": {
                    "type": "string",
                    "description": "清除范围。可选：all（默认）、category:<名称>、tag:<名称>。"
                },
                "limit": {
                    "type": "integer",
                    "description": "list/search 返回上限。默认 50，最大 500。",
                    "default": 50
                },
                "source": {
                    "type": "string",
                    "description": "记忆来源。agent（默认）或 user。"
                }
            },
            "required": ["action"]
        },
        "input_examples": [
            {"action": "add", "content": "用户对花生过敏", "category": "fact", "importance": "high", "tags": ["健康", "过敏"]},
            {"action": "search", "query": "过敏"},
            {"action": "list", "category": "preference"},
            {"action": "update", "memory_id": "3", "content": "用户对花生和海鲜过敏", "importance": "high"},
            {"action": "delete", "memory_id": "5"},
            {"action": "clear", "scope": "tag:temp"}
        ]
    }
}
