"""Static regression checks for draft cleanup and rich-only user output."""
import ast
from pathlib import Path

APP = Path("src/apitelegramchat/app.py").read_text(encoding="utf-8")
UTILS = Path("src/apitelegramchat/utils.py").read_text(encoding="utf-8")
HANDLERS = Path("src/apitelegramchat/ai_handlers.py").read_text(encoding="utf-8")

# Keep source parseable before making any behavioural assertions.
ast.parse(APP)
ast.parse(UTILS)
ast.parse(HANDLERS)

interrupt_start = APP.index("async def _interrupt_active_generation")
interrupt_end = APP.index("# ---------------------------------------------------------------------------", interrupt_start)
interrupt = APP[interrupt_start:interrupt_end]
assert "await _cancel_old_task(chat_id)" in interrupt
assert "await mark_draft_dead(draft_id)" in interrupt
assert "await clear_active_draft(chat_id, draft_id)" in interrupt
assert "await delete_message(chat_id, message_id)" in interrupt
assert "mark_preserved_draft" not in interrupt
assert interrupt.index("await clear_active_draft") < interrupt.index("await delete_message")

# Every successful builder flush re-registers draft_id -> real message_id. This
# is required after a rollover because its initial registration uses message_id=0.
assert "await state.set_active_draft(self.chat_id, self.draft_id, msg_id)" in HANDLERS

# The production app must not emit user-visible classic Bot API sendMessage.
assert 'f"{BASE_URL}/sendMessage"' not in APP
assert "_send_via_send_message" not in APP
assert "_send_rich_command_message" in APP

# Rich draft reassert and all rich send paths use one InputRichMessage field.
assert '"content": html_content,\n                "html": html_content' not in UTILS
assert UTILS.count('"rich_message": {"html": html_content}') == 4

print("draft cleanup and rich-only output validation passed")
