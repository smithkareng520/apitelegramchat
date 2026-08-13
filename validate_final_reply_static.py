"""Static checks for one-shot rich final replies and interim-output isolation."""
import ast
from pathlib import Path

SOURCE = Path("src/apitelegramchat/ai_handlers.py").read_text(encoding="utf-8")
ast.parse(SOURCE)

# Tool-call turns may stream provider-private analysis through `content`, but it
# must be discarded before the assistant tool-protocol message is constructed.
assert "round_block_start = len(builder.blocks)" in SOURCE
assert "builder.discard_interim_agent_output(round_block_start)" in SOURCE
assert 'content_acc = ""' in SOURCE
assert 'reasoning_acc = ""' in SOURCE

# The final draft transcript is UI-only. It must never be rolled over and made
# permanent as a surrogate answer.
assert "terminal_segment_rolled" not in SOURCE
assert "最终提交只以最终模型 content 为准" in SOURCE

# The final answer is normalized to rich HTML and sent exactly from final segments
# with reassert disabled, preventing a second draft or plain-text fallback.
assert "final_segments = builder._split_html_for_rich_messages(final_html)" in SOURCE
assert "send_rich_html_message(chat_id, segment_html, reassert_draft=False)" in SOURCE
assert 'final_html = f"<p>{escaped}</p>"' in SOURCE

print("final reply isolation and rich-only completion validation passed")
