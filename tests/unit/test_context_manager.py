'''Unit tests for context manager.'''

import context_manager as cm
from core.messages import Message, TextBlock


def _text_message(text: str) -> Message:
    return Message(role="user", blocks=[TextBlock(text)])


def test_fit_message_to_token_budget_keeps_result_bounded(monkeypatch):
    original = "0123456789" * 500
    message = _text_message(original)

    # Use a cheap deterministic counter so this test measures the search strategy,
    # not tokenizer behavior.
    calls = {"count": 0}

    def fake_count(value):
        calls["count"] += 1
        if isinstance(value, Message):
            return len(value.text()) + 1
        return len(str(value))

    monkeypatch.setattr(cm, "_message_token_count", fake_count)
    fitted = cm._fit_message_to_token_budget(message, 128)

    assert isinstance(fitted, Message)
    assert fake_count(fitted) <= 128
    assert fitted.text().startswith(original[:20])
    # Binary search should stay logarithmic instead of decrementing one token at a time.
    assert calls["count"] < 20


def test_select_request_context_truncates_single_oversized_message(monkeypatch):
    message = _text_message("x" * 500)
    monkeypatch.setattr(cm, "_message_token_count", lambda value: len(value.text()) + 1)

    snapshot = cm.select_request_context([message], max_tokens=64)

    assert len(snapshot.messages) == 1
    assert snapshot.estimated_tokens <= 64
    assert snapshot.messages[0].text().startswith("x")
