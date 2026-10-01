from __future__ import annotations

from core.messages import Message, TextBlock, ReasoningBlock, ToolCallBlock
from ai.response_protocol import (
    RESPONSES_OUTPUT_ITEMS_META_KEY,
    RESPONSES_OUTPUT_MODEL_META_KEY,
    attach_output_items_to_message,
    message_to_responses_input_items,
    messages_to_responses_request,
    output_items_to_input_items,
    output_text_from_items,
    response_function_calls_to_chat,
    tools_to_responses,
)


def test_response_output_items_are_replayed_without_collapsing_native_fields():
    items = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "thinking"}],
            "encrypted_content": "opaque-encrypted-payload",
        },
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "phase": "final",
            "content": [{
                "type": "output_text",
                "text": "done",
                "annotations": [{"type": "url_citation", "url": "https://example.com"}],
            }],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "search",
            "arguments": '{"q":"openai"}',
            "status": "completed",
            "namespace": "tools",
        },
    ]

    replay = output_items_to_input_items(items)
    assert replay == items
    assert replay is not items
    replay[0]["encrypted_content"] = "changed-locally"
    assert items[0]["encrypted_content"] == "opaque-encrypted-payload"


def test_completed_function_calls_only_are_executable():
    items = [
        {"type": "function_call", "id": "fc_1", "call_id": "c1", "name": "ok", "arguments": "{}", "status": "completed"},
        {"type": "function_call", "id": "fc_2", "call_id": "c2", "name": "bad", "arguments": "{}", "status": "incomplete"},
        {"type": "function_call", "id": "fc_3", "call_id": "c3", "name": "pending", "arguments": "{}", "status": "in_progress"},
    ]
    calls = response_function_calls_to_chat(items)
    assert [c["id"] for c in calls] == ["c1"]
    assert calls[0]["function"]["name"] == "ok"


def test_refusal_and_output_text_are_recovered_from_native_output_items():
    items = [{
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "refusal", "refusal": "cannot comply"},
        ],
    }]
    assert output_text_from_items(items) == "cannot comply"


def test_assistant_native_snapshot_is_used_only_for_same_model_and_matching_message():
    message = Message.assistant_with_tool_calls(
        "done",
        [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"q":"openai"}'},
        }],
    )
    native = [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search", "arguments": '{"q": "openai"}', "status": "completed"},
    ]
    attach_output_items_to_message(message, native, model="gpt-test")

    assert message.meta[RESPONSES_OUTPUT_MODEL_META_KEY] == "gpt-test"
    assert message.meta[RESPONSES_OUTPUT_ITEMS_META_KEY] == native
    assert message_to_responses_input_items(message, model="gpt-test") == native

    # The complete native output is also retained across model switches, as
    # required for stateless/bootstrap replay.
    assert message_to_responses_input_items(message, model="other-model") == native

    message.blocks[1] = ToolCallBlock(id="call_1", name="search", arguments={"q": "different"})
    assert message_to_responses_input_items(message, model="gpt-test") != native


def test_messages_to_responses_request_keeps_system_in_instructions_and_native_items_in_input():
    assistant = Message.assistant_text("answer", reasoning="summary")
    user = Message.user_text("question")
    instructions, input_items = messages_to_responses_request(
        [Message.system("system prompt"), user, assistant], model="gpt-test"
    )
    assert instructions == "system prompt"
    assert input_items[0] == {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "question"}]}
    # A locally created assistant message has no native snapshot, so its
    # reasoning block is not silently sent as a Chat Completions field.
    assert input_items[1] == {"role": "assistant", "content": "answer"}


def test_tools_to_responses_preserves_non_function_responses_native_tools():
    native = {"type": "web_search_preview", "search_context_size": "high"}
    assert tools_to_responses([native]) == [native]


def test_tools_to_responses_flattens_chat_function_definitions():
    tools = [{
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            "strict": True,
        },
    }]
    assert tools_to_responses(tools) == [{
        "type": "function",
        "name": "search",
        "description": "Search",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        "strict": True,
    }]


def test_tool_result_is_rendered_as_responses_function_call_output():
    tool_msg = Message.tool_result("call_123", "internal_search", '{"ok":true}')
    assert message_to_responses_input_items(tool_msg, model="gpt-test") == [{
        "type": "function_call_output",
        "call_id": "call_123",
        "output": '{"ok":true}',
    }]
