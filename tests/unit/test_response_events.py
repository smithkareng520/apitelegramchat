from types import SimpleNamespace

from ai.response_events import (
    event_field,
    event_type,
    incomplete_reason,
    merge_function_call_item,
    plain_response_item,
    response_error_message,
    response_output_items,
)


def test_function_call_done_snapshot_overwrites_partial_arguments():
    states = {}
    first = SimpleNamespace(id="item_1", call_id="call_1", name="search", arguments="")
    _, state, created = merge_function_call_item(states, first)
    assert created is True
    state["args_json"] = '{"q":"par'
    done = SimpleNamespace(
        id="item_1", call_id="call_1", name="search",
        arguments='{"q":"partial-safe"}',
    )
    _, state, created = merge_function_call_item(states, done)
    assert created is False
    assert state["args_json"] == '{"q":"partial-safe"}'


def test_empty_done_arguments_do_not_erase_delta():
    states = {"item_1": {"call_id": "call_1", "name": "search", "args_json": '{"q":"ok"}'}}
    done = SimpleNamespace(id="item_1", call_id="call_1", name="search", arguments="")
    merge_function_call_item(states, done)
    assert states["item_1"]["args_json"] == '{"q":"ok"}'


def test_incomplete_reason_is_safe_for_missing_fields():
    assert incomplete_reason(SimpleNamespace(incomplete_details=None), "incomplete") == "incomplete"
    assert incomplete_reason(
        SimpleNamespace(incomplete_details=SimpleNamespace(reason="max_output_tokens")),
        "incomplete",
    ) == "max_output_tokens"


def test_event_helpers_accept_raw_dict_payloads():
    event = {"type": "response.failed", "message": "boom", "response": {"output": [{"type": "message"}]}}
    assert event_type(event) == "response.failed"
    assert event_field(event, "message") == "boom"
    assert response_error_message(event, event["response"]) == "boom"
    assert response_output_items(event["response"]) == [{"type": "message"}]
    assert plain_response_item({"type": "reasoning", "encrypted_content": "opaque"}) == {
        "type": "reasoning", "encrypted_content": "opaque"
    }
