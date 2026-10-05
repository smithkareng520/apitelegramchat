"""回归：Gemini 要求 type=array 必须带 items，缺失即整请求 400。"""
from ai.gemini_bridge import _clean_schema_for_gemini, _convert_tools_to_gemini
from message_user_tool import MESSAGE_USER_TOOL


def _walk_arrays(node):
    if isinstance(node, dict):
        if node.get("type") == "array":
            yield node
        for v in node.values():
            yield from _walk_arrays(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_arrays(v)


def test_array_without_items_gets_default():
    out = _clean_schema_for_gemini({"type": "object", "properties": {"x": {"type": "array"}}})
    assert out["properties"]["x"]["items"] == {"type": "string"}


def test_array_with_items_preserved():
    out = _clean_schema_for_gemini({"type": "array", "items": {"type": "integer"}})
    assert out["items"] == {"type": "integer"}


def test_ask_user_tool_arrays_all_have_items():
    decls = _convert_tools_to_gemini([MESSAGE_USER_TOOL])[0]["functionDeclarations"]
    arrays = list(_walk_arrays(decls))
    assert arrays and all(isinstance(a.get("items"), dict) for a in arrays)


def _builtin_defs():
    from tool_registry import builtin_tool_defs
    return builtin_tool_defs()


def test_all_builtin_tool_sources_declare_array_items():
    """源头 schema 对所有协议（OpenAI/Responses/Anthropic/Gemini）一致：数组必须声明 items。"""
    for tool in _builtin_defs():
        params = tool["function"].get("parameters") or {}
        for arr in _walk_arrays(params):
            assert isinstance(arr.get("items"), (dict, list)), tool["function"]["name"]


def test_all_builtin_tools_convert_for_gemini():
    decls = _convert_tools_to_gemini(_builtin_defs())[0]["functionDeclarations"]
    for arr in _walk_arrays(decls):
        assert isinstance(arr.get("items"), dict)


def test_message_user_has_no_legacy_top_level_fields():
    props = MESSAGE_USER_TOOL["function"]["parameters"]["properties"]
    assert set(props) == {"mode", "message", "questions"}
