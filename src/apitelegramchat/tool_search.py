"""Client-side tool discovery compatible with OpenAI-style function calling.

The Anthropic tool-search API returns native ``tool_reference`` blocks.  This
application talks to OpenAI-compatible providers, so this module implements the
same *workflow* on the client side: only a compact eager tool set is sent to the
model, ``tool_search`` discovers relevant definitions, and the agent loop adds
the returned tool names before its next model turn.

The module is deliberately dependency-free.  It supports a lightweight BM25
ranker for natural-language requests and a bounded, case-insensitive Python
regular-expression mode for exact/pattern discovery.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

MAX_QUERY_LENGTH = 500
MAX_REGEX_LENGTH = 200
DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 5

# A Unicode-friendly tokenizer.  It retains identifiers (``fetch_url``), words,
# numbers and individual CJK characters so Chinese and English queries both have
# useful recall without an external NLP dependency.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]", re.UNICODE)
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9]+", re.UNICODE)


TOOL_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "tool_search",
        "description": (
            "Search the deferred tool catalog and load only the tools needed for the current task. "
            "Use this before attempting a capability that is not currently available. "
            "BM25 accepts a concise natural-language intent; regex accepts a case-insensitive Python "
            "regular-expression pattern. This tool only discovers tools and never performs the task itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "_description": {
                    "type": "string",
                    "description": "简述本次查找目的（≤60字）。示例：查找天气与路线规划工具"
                },
                "query": {
                    "type": "string",
                    "description": "BM25 模式下为自然语言需求；regex 模式下为 Python 正则表达式。"
                },
                "strategy": {
                    "type": "string",
                    "enum": ["bm25", "regex"],
                    "default": "bm25",
                    "description": "bm25 适合按任务意图检索；regex 适合按工具名称或固定词匹配。"
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "default": 5,
                    "description": "最多返回的工具数，默认 5。"
                }
            },
            "required": ["query"]
        },
        "input_examples": [
            {"query": "获取某城市实时天气和预报", "strategy": "bm25"},
            {"query": "^(web_search|fetch_url)$", "strategy": "regex", "max_results": 2}
        ]
    }
}


@dataclass(frozen=True)
class _CatalogEntry:
    name: str
    definition: dict[str, Any]
    description: str
    parameter_text: str
    searchable_text: str
    tokens: Counter[str]
    length: int


def _function_name(tool: dict[str, Any]) -> str:
    function = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(function, dict):
        return ""
    value = function.get("name")
    return value.strip() if isinstance(value, str) else ""


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _tokens(value: str) -> list[str]:
    raw = [item.lower() for item in _TOKEN_RE.findall(value or "")]
    # ``fetch_url`` should match both the complete function name and each word.
    expanded: list[str] = []
    for item in raw:
        expanded.append(item)
        if "_" in item:
            expanded.extend(part for part in item.split("_") if part)
    return expanded


def _parameter_text(function: dict[str, Any]) -> str:
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        return ""
    fragments: list[str] = []
    properties = parameters.get("properties")
    if isinstance(properties, dict):
        for key, schema in properties.items():
            fragments.append(str(key))
            if isinstance(schema, dict):
                fragments.append(_as_text(schema.get("description")))
                fragments.append(_as_text(schema.get("enum")))
    return " ".join(fragment for fragment in fragments if fragment)


class ToolCatalog:
    """Immutable, searchable catalog of OpenAI function definitions."""

    def __init__(self, tools: Iterable[dict[str, Any]]) -> None:
        entries: list[_CatalogEntry] = []
        by_name: dict[str, _CatalogEntry] = {}
        for raw in tools:
            if not isinstance(raw, dict):
                continue
            name = _function_name(raw)
            if not name or name in by_name:
                continue
            function = raw.get("function", {})
            description = _as_text(function.get("description"))
            parameter_text = _parameter_text(function)
            examples = _as_text(function.get("input_examples"))
            searchable_text = " ".join((name, name.replace("_", " "), description, parameter_text, examples))
            counts = Counter(_tokens(searchable_text))
            entry = _CatalogEntry(
                name=name,
                definition=copy.deepcopy(raw),
                description=description,
                parameter_text=parameter_text,
                searchable_text=searchable_text,
                tokens=counts,
                length=max(1, sum(counts.values())),
            )
            entries.append(entry)
            by_name[name] = entry
        self._entries = tuple(entries)
        self._by_name = by_name
        document_count = max(1, len(entries))
        df: Counter[str] = Counter()
        for entry in entries:
            df.update(entry.tokens.keys())
        self._idf = {
            token: math.log(1.0 + (document_count - frequency + 0.5) / (frequency + 0.5))
            for token, frequency in df.items()
        }
        self._average_length = (
            sum(entry.length for entry in entries) / len(entries)
            if entries else 1.0
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self._entries)

    def definition(self, name: str) -> dict[str, Any] | None:
        entry = self._by_name.get(name)
        return copy.deepcopy(entry.definition) if entry else None

    def definitions(self, names: Iterable[str]) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                continue
            entry = self._by_name.get(name)
            if entry is not None:
                selected.append(copy.deepcopy(entry.definition))
                seen.add(name)
        return selected

    def category_summary(self, max_names: int = 18) -> str:
        names = list(self.names)
        if not names:
            return "当前没有可检索的延迟工具。"
        visible = ", ".join(names[:max_names])
        suffix = " 等" if len(names) > max_names else ""
        return f"可按需检索的工具涵盖：{visible}{suffix}。"

    def search(self, query: str, strategy: str = "bm25", limit: int = DEFAULT_RESULT_LIMIT) -> dict[str, Any]:
        query = (query or "").strip()
        strategy = (strategy or "bm25").strip().lower()
        if not query:
            return _error_payload("empty_query", "工具搜索 query 不能为空。")
        if strategy not in {"bm25", "regex"}:
            return _error_payload("bad_strategy", "strategy 只能是 bm25 或 regex。")
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = DEFAULT_RESULT_LIMIT
        limit = max(1, min(limit, MAX_RESULT_LIMIT))

        if strategy == "regex":
            return self._search_regex(query, limit)
        return self._search_bm25(query, limit)

    def _search_regex(self, query: str, limit: int) -> dict[str, Any]:
        if len(query) > MAX_REGEX_LENGTH:
            return _error_payload(
                "invalid_tool_input",
                f"正则表达式长度不能超过 {MAX_REGEX_LENGTH} 个字符。",
            )
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            return _error_payload("invalid_tool_input", f"无效的正则表达式：{exc}")

        scored: list[tuple[float, _CatalogEntry, list[str]]] = []
        for entry in self._entries:
            fields = _matching_fields(entry, pattern)
            if fields:
                # Favor exact name matches while preserving a deterministic order.
                name_bonus = 30.0 if pattern.search(entry.name) else 0.0
                scored.append((name_bonus + len(fields), entry, fields))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return _success_payload(query, "regex", scored, limit)

    def _search_bm25(self, query: str, limit: int) -> dict[str, Any]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return _error_payload("empty_query", "查询中没有可检索的文字或标识符。")
        query_terms = Counter(query_tokens)
        k1 = 1.5
        b = 0.75
        scored: list[tuple[float, _CatalogEntry, list[str]]] = []
        normalized_query = query.lower()
        for entry in self._entries:
            score = 0.0
            for term, qtf in query_terms.items():
                tf = entry.tokens.get(term, 0)
                if not tf:
                    continue
                idf = self._idf.get(term, 0.0)
                denominator = tf + k1 * (1.0 - b + b * entry.length / self._average_length)
                score += qtf * idf * (tf * (k1 + 1.0) / denominator)
            # Query/name phrase agreement is especially valuable for tool choice.
            compact_query = normalized_query.replace(" ", "_")
            if normalized_query in entry.name.lower() or compact_query in entry.name.lower():
                score += 3.0
            if normalized_query and normalized_query in entry.description.lower():
                score += 1.2
            if score > 0:
                scored.append((score, entry, _matching_bm25_fields(entry, query_terms)))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return _success_payload(query, "bm25", scored, limit)


def _matching_fields(entry: _CatalogEntry, pattern: re.Pattern[str]) -> list[str]:
    fields: list[str] = []
    if pattern.search(entry.name):
        fields.append("name")
    if pattern.search(entry.description):
        fields.append("description")
    if pattern.search(entry.parameter_text):
        fields.append("parameters")
    return fields


def _matching_bm25_fields(entry: _CatalogEntry, query_terms: Counter[str]) -> list[str]:
    matched = set(query_terms).intersection(entry.tokens)
    fields: list[str] = []
    if any(term in _tokens(entry.name) for term in matched):
        fields.append("name")
    if any(term in _tokens(entry.description) for term in matched):
        fields.append("description")
    if any(term in _tokens(entry.parameter_text) for term in matched):
        fields.append("parameters")
    return fields or ["catalog"]


def _reference(entry: _CatalogEntry, score: float, matched_fields: list[str]) -> dict[str, Any]:
    function = entry.definition.get("function", {})
    return {
        "type": "tool_reference",
        "tool_name": entry.name,
        "score": round(score, 3),
        "matched_fields": matched_fields,
        "description": function.get("description", ""),
        "input_schema": function.get("parameters", {}),
    }


def _success_payload(
    query: str,
    strategy: str,
    scored: list[tuple[float, _CatalogEntry, list[str]]],
    limit: int,
) -> dict[str, Any]:
    references = [_reference(entry, score, fields) for score, entry, fields in scored[:limit]]
    return {
        "ok": True,
        "action": "tool_search",
        "query": query,
        "strategy": strategy,
        "matched": len(scored),
        "returned": len(references),
        "tool_references": references,
        "loaded_tool_names": [item["tool_name"] for item in references],
        "message": (
            f"找到 {len(scored)} 个候选工具，已加载 {len(references)} 个。"
            if references else "未找到匹配工具。请换用更具体的能力、资源或参数关键词。"
        ),
    }


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "action": "tool_search",
        "code": code,
        "error": message,
        "tool_references": [],
        "loaded_tool_names": [],
    }


def execute_tool_search(
    query: str,
    catalog: ToolCatalog,
    strategy: str = "bm25",
    max_results: int = DEFAULT_RESULT_LIMIT,
) -> str:
    """Run a search and serialize a standard tool-result payload for the LLM."""
    payload = catalog.search(query=query, strategy=strategy, limit=max_results)
    return json.dumps(payload, ensure_ascii=False)


def extract_loaded_tool_names(result: str, catalog: ToolCatalog) -> list[str]:
    """Return only verified catalog names from a tool-search result.

    Tool output is model-visible and should be treated as untrusted when it is
    subsequently parsed by the orchestrator.  This function validates every name
    against the immutable server-side catalog before making it callable.
    """
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict) or not payload.get("ok"):
        return []
    raw_names = payload.get("loaded_tool_names")
    if not isinstance(raw_names, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in raw_names:
        name = item.strip() if isinstance(item, str) else ""
        if name and name not in seen and catalog.definition(name) is not None:
            names.append(name)
            seen.add(name)
    return names


def render_tool_search_card(payload: dict[str, Any]) -> str:
    """Render a concise, safe Telegram HTML card for discovery results."""
    def esc(value: Any) -> str:
        return str(value if value is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    if not isinstance(payload, dict) or not payload.get("ok"):
        message = payload.get("error", "未知错误") if isinstance(payload, dict) else str(payload)
        return f"<p><b>工具搜索未完成</b></p><blockquote>{esc(message)}</blockquote>"

    query = esc(payload.get("query", ""))
    strategy = esc(payload.get("strategy", "bm25")).upper()
    references = payload.get("tool_references") or []
    header = (
        f"<p><b>工具搜索</b> · <code>{strategy}</code></p>"
        f"<p>查询：<code>{query}</code> · 命中 <b>{esc(payload.get('matched', 0))}</b> · 已加载 <b>{esc(payload.get('returned', 0))}</b></p>"
    )
    if not references:
        return header + "<blockquote>没有匹配工具。可改用更具体的任务、资源或参数关键词。</blockquote>"

    items: list[str] = []
    for ref in references[:MAX_RESULT_LIMIT]:
        if not isinstance(ref, dict):
            continue
        name = esc(ref.get("tool_name", "?"))
        description = esc(ref.get("description", ""))
        if len(description) > 220:
            description = description[:220] + "…"
        matched = ", ".join(esc(item) for item in (ref.get("matched_fields") or [])[:3])
        score = esc(ref.get("score", ""))
        meta = " · ".join(part for part in (f"匹配：{matched}" if matched else "", f"相关度 {score}" if score else "") if part)
        items.append(f"<li><b><code>{name}</code></b><br/>{description}<br/><i>{meta}</i></li>")
    return header + "<ol>" + "".join(items) + "</ol><p><i>已发现的工具将在下一轮可调用；搜索本身不会执行任务。</i></p>"
