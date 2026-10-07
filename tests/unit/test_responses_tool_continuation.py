'''Unit tests for responses tool continuation.'''

from core.messages import Message
from ai.response_protocol import message_to_responses_input_items, messages_to_responses_request


def test_tool_continuation_is_exact_responses_function_call_output():
    msg = Message.tool_result("call_search_123", "internal_search", '{"results":[1,2]}')
    items = messages_to_responses_request([msg], model="muse-spark-1.3-contributor")[1]
    assert items == [{
        "type": "function_call_output",
        "call_id": "call_search_123",
        "output": '{"results":[1,2]}',
    }]
    assert items[0]["type"] == "function_call_output"
    assert items[0]["call_id"] == "call_search_123"
    assert items[0]["output"]


def test_multiple_tool_outputs_preserve_call_ids_and_order():
    messages = [
        Message.tool_result("call_a", "internal_search", "A"),
        Message.tool_result("call_b", "fetch_url", "B"),
    ]
    _, items = messages_to_responses_request(messages, model="muse-spark-1.3-contributor")
    assert [item["call_id"] for item in items] == ["call_a", "call_b"]
    assert [item["output"] for item in items] == ["A", "B"]
    assert all(item["type"] == "function_call_output" for item in items)


def test_empty_tool_result_content_is_still_a_nonempty_input_item():
    msg = Message.tool_result("call_empty", "internal_search", "")
    items = message_to_responses_input_items(msg, model="muse-spark-1.3-contributor")
    assert len(items) == 1
    assert items[0]["type"] == "function_call_output"
    assert items[0]["call_id"] == "call_empty"
    assert "output" in items[0]
