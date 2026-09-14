import asyncio

import state


def test_responses_conversation_state_lifecycle():
    chat_id = 991234
    ctx = state.get_or_init_context(chat_id)
    ctx["conversation_history"] = ["hello"]
    state.set_responses_conversation_id(chat_id, "conv_test")
    assert state.get_responses_conversation_id(chat_id) == "conv_test"

    asyncio.run(state.safe_clear_history(chat_id))

    ctx = state.get_or_init_context(chat_id)
    assert ctx["conversation_history"] == []
    assert state.get_responses_conversation_id(chat_id) is None
    assert ctx.get("openai_responses_canonical_fingerprint") is None


def test_responses_conversation_lock_is_shared_per_chat():
    async def run():
        a = await state.get_responses_conversation_lock(12345)
        b = await state.get_responses_conversation_lock(12345)
        c = await state.get_responses_conversation_lock(12346)
        assert a is b
        assert a is not c
    asyncio.run(run())
