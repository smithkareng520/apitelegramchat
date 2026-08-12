"""Explicit, policy-aware MCP tool catalogue.

The MCP protocol adapter does not infer schemas from Python signatures.  Tools
are declared with deterministic JSON Schemas, and high-impact tools are absent
unless the deployment explicitly opts in.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import mcp.types as types

from apitelegramchat.mcp.context import MCPRequestContext, mutations_are_explicitly_enabled

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]
ToolHandler = Callable[[MCPRequestContext, JsonObject], Awaitable[str] | str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: JsonObject
    handler: ToolHandler

    def as_mcp_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            title=self.title,
            description=self.description,
            inputSchema=self.input_schema,
        )


def object_schema(properties: JsonObject, required: tuple[str, ...] = ()) -> JsonObject:
    schema: JsonObject = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def text_field(description: str, min_length: int | None = None) -> JsonObject:
    field: JsonObject = {"type": "string", "description": description}
    if min_length is not None:
        field["minLength"] = min_length
    return field


def int_field(description: str, minimum: int | None = None, maximum: int | None = None) -> JsonObject:
    field: JsonObject = {"type": "integer", "description": description}
    if minimum is not None:
        field["minimum"] = minimum
    if maximum is not None:
        field["maximum"] = maximum
    return field


async def invoke(function: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    result = function(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return str(result)


async def web_search(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_web_search
    return await invoke(execute_web_search, args["query"], args.get("num_results", 5))


async def fetch_url(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_fetch_url
    return await invoke(execute_fetch_url, args["url"])


async def wikipedia(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_wikipedia
    return await invoke(execute_wikipedia, args["query"], args.get("lang", "zh"))


async def exchange_rate(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_exchange_rate
    return await invoke(execute_exchange_rate, args["base"], args.get("target"))


async def book_lookup(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_book_lookup
    return await invoke(execute_book_lookup, args["query"])


async def weather(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_weather
    return await invoke(execute_weather, args["city"], args.get("unit", "c"), args.get("hours", 6))


async def news(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_news
    return await invoke(execute_news, args.get("source", "bbc"), args.get("limit", 5))


async def crypto(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_crypto_price
    return await invoke(execute_crypto_price, args["coin"], args.get("currency", "usd"))


async def ip_geo(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_ip_geo
    return await invoke(execute_ip_geo, args.get("ip"))


async def geocode(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_geocode
    return await invoke(execute_geocode, args["address"])


async def route(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_route
    return await invoke(
        execute_route,
        args["origin"],
        args["destination"],
        args.get("mode", "driving"),
        args.get("city"),
        args.get("cityd"),
    )


async def distance(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_distance
    return await invoke(execute_distance, args["origin"], args["destination"])


async def poi_keyword(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_keyword_search
    return await invoke(execute_keyword_search, args["keywords"], args.get("city"))


async def poi_nearby(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_nearby_search
    return await invoke(execute_nearby_search, args["keywords"], args["location"], args.get("radius"))


async def poi_details(_: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_poi_details
    return await invoke(execute_poi_details, args["id"])


async def list_download(context: MCPRequestContext, _: JsonObject) -> str:
    from apitelegramchat.tool_executors import execute_list_download
    return await invoke(execute_list_download, context.chat_id)


async def list_upload(context: MCPRequestContext, _: JsonObject) -> str:
    from apitelegramchat.tool_executors import execute_list_upload
    return await invoke(execute_list_upload, context.chat_id)


async def workspace_view(context: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_file_editor
    return await invoke(
        execute_file_editor,
        chat_id=context.chat_id,
        namespace=context.scope,
        command=args["command"],
        path=args.get("path", "."),
        view_range=args.get("view_range"),
        search_terms=args.get("search_terms"),
    )


async def memory_manage(context: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.memory_tool import execute_memory
    return await invoke(execute_memory, context.chat_id, **args)


async def todo_manage(context: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.todo_tool import execute_todo
    return await invoke(execute_todo, context.chat_id, **args)


async def workspace_edit(context: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.search_engine import execute_file_editor
    return await invoke(execute_file_editor, chat_id=context.chat_id, namespace=context.scope, **args)


async def fetch_download(context: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.tool_executors import execute_fetch_download
    return await invoke(execute_fetch_download, context.chat_id, args["filenames"], overwrite=args.get("overwrite", False))


async def stage_upload(context: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.tool_executors import execute_stage_upload
    return await invoke(execute_stage_upload, context.chat_id, args["paths"])


async def present_files(context: MCPRequestContext, args: JsonObject) -> str:
    from apitelegramchat.tool_executors import execute_present_files
    return await invoke(execute_present_files, context.chat_id, args["paths"])


READ_ONLY_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("search.web", "Web search", "Search public web information.", object_schema({"query": text_field("Search query.", 1), "num_results": int_field("Maximum result count.", 1, 10)}, ("query",)), web_search),
    ToolSpec("search.fetch", "Fetch URL", "Fetch and extract a public HTTP(S) URL.", object_schema({"url": text_field("HTTP(S) URL.", 8)}, ("url",)), fetch_url),
    ToolSpec("search.wikipedia", "Wikipedia", "Query Wikipedia.", object_schema({"query": text_field("Article query.", 1), "lang": {"type": "string", "enum": ["zh", "en"]}}, ("query",)), wikipedia),
    ToolSpec("search.exchange_rate", "Exchange rate", "Look up an exchange rate.", object_schema({"base": text_field("Base ISO currency.", 3), "target": text_field("Optional target ISO currency.", 3)}, ("base",)), exchange_rate),
    ToolSpec("search.book_lookup", "Book lookup", "Look up published books.", object_schema({"query": text_field("Book query.", 1)}, ("query",)), book_lookup),
    ToolSpec("search.weather", "Weather", "Retrieve weather information.", object_schema({"city": text_field("City or location.", 1), "unit": {"type": "string", "enum": ["c", "f"]}, "hours": int_field("Forecast hours.", 1, 24)}, ("city",)), weather),
    ToolSpec("search.news", "News", "Retrieve news summaries.", object_schema({"source": text_field("Optional source identifier."), "limit": int_field("Maximum result count.", 1, 10)}), news),
    ToolSpec("search.crypto_price", "Crypto price", "Retrieve a cryptocurrency price.", object_schema({"coin": text_field("Coin name or symbol.", 1), "currency": text_field("Quote currency.", 3)}, ("coin",)), crypto),
    ToolSpec("search.ip_geo", "IP geolocation", "Look up a supplied IP address.", object_schema({"ip": text_field("Optional IP address.")}), ip_geo),
    ToolSpec("geo.geocode", "Geocode", "Convert an address to coordinates.", object_schema({"address": text_field("Address.", 1)}, ("address",)), geocode),
    ToolSpec("geo.route", "Route", "Plan a route between longitude,latitude coordinates.", object_schema({"origin": text_field("Origin longitude,latitude.", 3), "destination": text_field("Destination longitude,latitude.", 3), "mode": {"type": "string", "enum": ["cycling", "walking", "driving", "transit"]}, "city": text_field("Optional origin city."), "cityd": text_field("Optional destination city.")}, ("origin", "destination")), route),
    ToolSpec("geo.distance", "Distance", "Measure straight-line coordinate distance.", object_schema({"origin": text_field("Origin longitude,latitude.", 3), "destination": text_field("Destination longitude,latitude.", 3)}, ("origin", "destination")), distance),
    ToolSpec("geo.poi_keyword_search", "POI keyword search", "Search points of interest by keyword.", object_schema({"keywords": text_field("Search keywords.", 1), "city": text_field("Optional city.")}, ("keywords",)), poi_keyword),
    ToolSpec("geo.poi_nearby_search", "Nearby POI search", "Search points of interest near coordinates.", object_schema({"keywords": text_field("Search keywords.", 1), "location": text_field("Longitude,latitude.", 3), "radius": int_field("Radius in metres.", 1, 50000)}, ("keywords", "location")), poi_nearby),
    ToolSpec("geo.poi_details", "POI details", "Retrieve point-of-interest details.", object_schema({"id": text_field("Point-of-interest identifier.", 1)}, ("id",)), poi_details),
    ToolSpec("workspace.list_download", "List downloaded files", "List files in the private download staging area.", object_schema({}), list_download),
    ToolSpec("workspace.list_upload", "List staged files", "List files in the private upload staging area.", object_schema({}), list_upload),
    ToolSpec("workspace.view", "View workspace", "List or view a private workspace path. Links are not followed outside the workspace.", object_schema({"command": {"type": "string", "enum": ["view", "list"]}, "path": text_field("Optional relative path."), "view_range": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}, "search_terms": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, ("command",)), workspace_view),
)


MUTATION_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("memory.manage", "Manage memory", "Mutate or query persistent private memory. Explicit deployment opt-in required.", object_schema({"action": {"type": "string", "enum": ["add", "get", "list", "search", "update", "delete", "clear"]}, "content": text_field("Memory content."), "memory_id": text_field("Memory identifier."), "category": text_field("Optional category."), "tags": {"type": ["array", "string"], "items": {"type": "string"}}, "importance": text_field("Optional importance."), "query": text_field("Optional query."), "scope": text_field("Optional memory scope."), "limit": int_field("Maximum result count.", 1, 100), "source": text_field("Optional source.")}), memory_manage),
    ToolSpec("todo.manage", "Manage todos", "Mutate or query private todos. Explicit deployment opt-in required.", object_schema({"action": {"type": "string", "enum": ["add", "list", "done", "undone", "delete", "clear", "edit"]}, "title": text_field("Todo title."), "todo_id": text_field("Todo identifier."), "priority": text_field("Optional priority."), "tags": {"type": ["array", "string"], "items": {"type": "string"}}, "note": text_field("Optional note."), "filter": text_field("Optional filter."), "tag": text_field("Optional tag.")}), todo_manage),
    ToolSpec("workspace.edit", "Edit workspace", "Create, update or delete a private workspace file. Explicit deployment opt-in required.", object_schema({"command": {"type": "string", "enum": ["create", "replace_lines", "str_replace", "insert", "delete", "undo_edit"]}, "path": text_field("Relative workspace file path.", 1), "old_str": text_field("Existing text."), "new_str": text_field("Replacement text."), "start_line": int_field("Start line.", 1), "end_line": int_field("End line.", 1), "occurrence": int_field("Occurrence.", 1), "allow_multi": {"type": "boolean"}, "use_regex": {"type": "boolean"}, "delete_range": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}, "insert_line": int_field("Insert after line.", 0), "insert_text": text_field("Text to insert."), "file_text": text_field("Content for new file."), "confirm": {"type": "boolean"}}, ("command", "path")), workspace_edit),
    ToolSpec("workspace.fetch_download", "Copy downloaded files", "Copy selected downloaded files into the workspace. Explicit deployment opt-in required.", object_schema({"filenames": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}, "overwrite": {"type": "boolean"}}, ("filenames",)), fetch_download),
    ToolSpec("workspace.stage_upload", "Stage outgoing files", "Stage selected workspace files. Explicit deployment opt-in required.", object_schema({"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}}, ("paths",)), stage_upload),
    ToolSpec("workspace.present", "Present staged files", "Present selected staged files. Explicit deployment opt-in required.", object_schema({"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}}, ("paths",)), present_files),
)


class ToolRegistry:
    """Expose a deterministic, least-privilege tool set for one trusted scope."""

    def __init__(self, context: MCPRequestContext) -> None:
        specs = list(READ_ONLY_SPECS)
        if mutations_are_explicitly_enabled():
            specs.extend(MUTATION_SPECS)
        self._context = context
        self._specs = tuple(specs)
        self._by_name = {spec.name: spec for spec in self._specs}

    async def list_tools(self) -> list[types.Tool]:
        return [spec.as_mcp_tool() for spec in self._specs]

    async def call(self, name: str, arguments: JsonObject) -> types.CallToolResult:
        spec = self._by_name.get(name)
        if spec is None:
            return self._error(f"Unknown or disabled tool: {name}")
        try:
            with self._context.activate():
                text = await spec.handler(self._context, arguments)
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=False)
        except Exception:
            logger.exception("MCP tool execution failed: %s", name)
            return self._error("Tool execution failed. Check the arguments and retry.")

    @staticmethod
    def _error(message: str) -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)
