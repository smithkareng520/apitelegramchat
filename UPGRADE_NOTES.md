# Draft and context management upgrade

## Draft lifecycle

Every new draft, including a rollover draft, starts with `<tg-thinking>Thinking...</tg-thinking>`.
The status is intentionally stable and English-only, so it cannot disappear while the next stream
chunk is pending or be replaced by a localized intermediate label.

Rollover remains ordered: completed content is sent permanently first, then the old preview is
retired, a new active draft ID is registered, and its first frame is forced immediately.  The new
frame contains both `Thinking...` and any uncommitted remainder, so there is no blank second draft.

## Agent request context

The full conversation remains stored.  For each model request, `context_manager.select_request_context`
creates a bounded snapshot before attachment resolution:

- newest valid messages are retained first;
- the default budget is 32 messages and 48,000 estimated characters;
- the snapshot never begins with an orphaned tool result;
- discarded-message count and retained size are logged for diagnostics.

This limits prompt growth and prevents old attachments from delaying request preparation, while
preserving the newest user intent.
