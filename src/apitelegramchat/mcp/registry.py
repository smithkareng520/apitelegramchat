from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, get_args, get_origin

from apitelegramchat.memory_tool import execute_memory
try:
    from apitelegramchat.subagent_tool import execute_subagent
except Exception:  # pragma: no cover - optional dependency fallback
    async def execute_subagent(*args, **kwargs):  # type: ignore
        return "Error: subagent tool is unavailable in this environment."
from apitelegramchat.todo_tool import execute_todo
from apitelegramchat.tool_executors import execute_bash, execute_present_files
from apitelegramchat.search_engine import (
    execute_book_lookup, execute_crypto_price, execute_done, execute_distance, execute_elevation,
    execute_exchange_rate, execute_fetch_url, execute_geocode, execute_generate_image,
    execute_generate_video, execute_hacker_news, execute_image_search, execute_ip_geo,
    execute_isochrone, execute_news, execute_place_details, execute_qr_code, execute_route,
    execute_search_poi, execute_text_editor, execute_weather, execute_web_search, execute_wikipedia,
)
from apitelegramchat.skills import catalog_text, read_skill_text
from apitelegramchat.workspace_paths import data_root, workspace_root
from ..core.settings import get_mcp_scope

logger = logging.getLogger("apitelegramchat.mcp")


def _chat_id() -> int:
    scope = get_mcp_scope().name
    import hashlib
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def _clean_args(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if v is not None}


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _schema_for(fn: Callable[..., Any], *, title: str | None = None) -> dict[str, Any]:
    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    def _type_to_schema(annotation: Any) -> str:
        origin = get_origin(annotation)
        args = get_args(annotation)
        if origin is list or origin is tuple or annotation in {list, list[str], list[int], tuple}:
            return "array"
        if origin is dict or annotation in {dict}:
            return "object"
        if origin is not None and args:
            for a in args:
                if a is not type(None):
                    return _type_to_schema(a)
        if annotation in {int, "int"}:
            return "integer"
        if annotation in {float, "float"}:
            return "number"
        if annotation in {bool, "bool"}:
            return "boolean"
        if annotation in {dict, "dict"}:
            return "object"
        return "string"

    for name, param in sig.parameters.items():
        if name in {"chat_id", "progress_callback"}:
            continue
        if param.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            continue
        schema: dict[str, Any] = {"description": f"{name} parameter", "type": _type_to_schema(param.annotation)}
        if param.default is not inspect._empty:
            schema["default"] = _jsonable(param.default)
        else:
            required.append(name)
        properties[name] = schema

    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": True}
    if required:
        schema["required"] = required
    if title:
        schema["title"] = title
    return schema


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., Awaitable[str] | str]
    schema: dict[str, Any]


async def _call(fn: Callable[..., Awaitable[str] | str], **kwargs: Any) -> Any:
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _tool_memory(**kwargs: Any) -> str:
    return await execute_memory(_chat_id(), **_clean_args(kwargs))


async def _tool_todo(**kwargs: Any) -> str:
    return await execute_todo(_chat_id(), **_clean_args(kwargs))


async def _tool_subagent(**kwargs: Any) -> str:
    return await execute_subagent(_chat_id(), **_clean_args(kwargs))


async def _tool_bash(**kwargs: Any) -> str:
    return await execute_bash(_chat_id(), **_clean_args(kwargs))


async def _tool_present_files(**kwargs: Any) -> str:
    return await execute_present_files(_chat_id(), **_clean_args(kwargs))


async def _tool_skill_catalog(**kwargs: Any) -> str:
    return catalog_text()


async def _tool_skill_read(skill_id: str, **kwargs: Any) -> str:
    return read_skill_text(skill_id)


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("memory.manage", "Manage persistent long-term memory.", _tool_memory, _schema_for(execute_memory, title="memory.manage")),
    ToolSpec("todo.manage", "Create, inspect, update, and clear todos.", _tool_todo, _schema_for(execute_todo, title="todo.manage")),
    ToolSpec("subagent.run", "Spawn a bounded subagent to complete a task.", _tool_subagent, _schema_for(execute_subagent, title="subagent.run")),
    ToolSpec("shell.exec", "Run a sandboxed shell command within the workspace.", _tool_bash, _schema_for(execute_bash, title="shell.exec")),
    ToolSpec("workspace.present", "Return workspace files to the client.", _tool_present_files, _schema_for(execute_present_files, title="workspace.present")),
    ToolSpec("skill.catalog", "INFO-ONLY: list skills discovered from .claude/skills. The runtime auto-activates the best match — do not call this to enable a skill.", _tool_skill_catalog, _schema_for(_tool_skill_catalog, title="skill.catalog")),
    ToolSpec("skill.read", "INFO-ONLY: read a skill body that was not auto-activated, for cross-skill reference. The auto-activated skill is already in <active_skill_context>.", _tool_skill_read, _schema_for(_tool_skill_read, title="skill.read")),
    ToolSpec("search.web", "Search the web.", lambda **kw: _call(execute_web_search, **_clean_args(kw)), _schema_for(execute_web_search, title="search.web")),
    ToolSpec("search.fetch", "Fetch and extract a URL.", lambda **kw: _call(execute_fetch_url, **_clean_args(kw)), _schema_for(execute_fetch_url, title="search.fetch")),
    ToolSpec("search.wikipedia", "Query Wikipedia.", lambda **kw: _call(execute_wikipedia, **_clean_args(kw)), _schema_for(execute_wikipedia, title="search.wikipedia")),
    ToolSpec("search.exchange_rate", "Get exchange rates.", lambda **kw: _call(execute_exchange_rate, **_clean_args(kw)), _schema_for(execute_exchange_rate, title="search.exchange_rate")),
    ToolSpec("search.hacker_news", "Fetch Hacker News stories.", lambda **kw: _call(execute_hacker_news, **_clean_args(kw)), _schema_for(execute_hacker_news, title="search.hacker_news")),
    ToolSpec("search.book_lookup", "Look up books.", lambda **kw: _call(execute_book_lookup, **_clean_args(kw)), _schema_for(execute_book_lookup, title="search.book_lookup")),
    ToolSpec("search.weather", "Fetch weather information.", lambda **kw: _call(execute_weather, **_clean_args(kw)), _schema_for(execute_weather, title="search.weather")),
    ToolSpec("search.news", "Fetch news summaries.", lambda **kw: _call(execute_news, **_clean_args(kw)), _schema_for(execute_news, title="search.news")),
    ToolSpec("search.crypto_price", "Fetch crypto prices.", lambda **kw: _call(execute_crypto_price, **_clean_args(kw)), _schema_for(execute_crypto_price, title="search.crypto_price")),
    ToolSpec("search.ip_geo", "Look up IP geolocation.", lambda **kw: _call(execute_ip_geo, **_clean_args(kw)), _schema_for(execute_ip_geo, title="search.ip_geo")),
    ToolSpec("search.qr_code", "Generate a QR code.", lambda **kw: _call(execute_qr_code, **_clean_args(kw)), _schema_for(execute_qr_code, title="search.qr_code")),
    ToolSpec("search.done", "Return a done marker.", lambda **kw: _call(execute_done, **_clean_args(kw)), _schema_for(execute_done, title="search.done")),
    ToolSpec("search.generate_image", "Generate an image.", lambda **kw: _call(execute_generate_image, **_clean_args(kw)), _schema_for(execute_generate_image, title="search.generate_image")),
    ToolSpec("search.generate_video", "Generate a video.", lambda **kw: _call(execute_generate_video, **_clean_args(kw)), _schema_for(execute_generate_video, title="search.generate_video")),
    ToolSpec("search.image_search", "Search images.", lambda **kw: _call(execute_image_search, **_clean_args(kw)), _schema_for(execute_image_search, title="search.image_search")),
    ToolSpec("geo.geocode", "Geocode an address.", lambda **kw: _call(execute_geocode, **_clean_args(kw)), _schema_for(execute_geocode, title="geo.geocode")),
    ToolSpec("geo.search_poi", "Search points of interest.", lambda **kw: _call(execute_search_poi, **_clean_args(kw)), _schema_for(execute_search_poi, title="geo.search_poi")),
    ToolSpec("geo.route", "Calculate a route.", lambda **kw: _call(execute_route, **_clean_args(kw)), _schema_for(execute_route, title="geo.route")),
    ToolSpec("geo.distance", "Calculate distance between two points.", lambda **kw: _call(execute_distance, **_clean_args(kw)), _schema_for(execute_distance, title="geo.distance")),
    ToolSpec("geo.place_details", "Fetch place details.", lambda **kw: _call(execute_place_details, **_clean_args(kw)), _schema_for(execute_place_details, title="geo.place_details")),
    ToolSpec("geo.elevation", "Fetch elevation data.", lambda **kw: _call(execute_elevation, **_clean_args(kw)), _schema_for(execute_elevation, title="geo.elevation")),
    ToolSpec("geo.isochrone", "Fetch isochrone contours.", lambda **kw: _call(execute_isochrone, **_clean_args(kw)), _schema_for(execute_isochrone, title="geo.isochrone")),
    ToolSpec("workspace.editor", "Edit workspace files with guarded operations.", lambda **kw: _call(execute_text_editor, **_clean_args(kw)), _schema_for(execute_text_editor, title="workspace.editor")),
]

TOOL_MAP = {spec.name: spec for spec in TOOL_SPECS}


async def list_tools() -> list[dict[str, Any]]:
    return [{"name": spec.name, "description": spec.description, "inputSchema": spec.schema} for spec in TOOL_SPECS]


async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    spec = TOOL_MAP.get(name)
    if not spec:
        return {"error": {"code": -32601, "message": f"Unknown tool: {name}"}}
    try:
        result = await spec.fn(**_clean_args(arguments))
        return {"content": [{"type": "text", "text": str(result)}]}
    except Exception as exc:
        logger.exception("tool call failed: %s", name)
        return {"error": {"code": -32000, "message": str(exc)}}
