"""Focused regression checks for the token-based context policy.

Run from the repository root with:
    python3 validate_token_context.py
"""
from apitelegramchat.agent_context import (
    CHECKPOINT_MARKER,
    compact_active_agent_context,
    compact_turn_for_history,
    estimate_messages_tokens,
    token_budget_for_model,
    trim_completed_history_to_budget,
)


class SmallModel:
    max_context = 8_000
    max_output_tokens = 1_000


def long_tool_trace(steps: int) -> list[dict]:
    records: list[dict] = []
    for index in range(steps):
        records.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {"name": "workspace.view", "arguments": '{"path":"report.md"}'},
                    }
                ],
            }
        )
        records.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": "workspace.view",
                "content": "result " + ("x" * 1_000),
            }
        )
    records.append({"role": "assistant", "content": "任务已完成：报告已写入 output/report.md。"})
    return records


def main() -> None:
    model = SmallModel()
    user = {"role": "user", "content": "请研究资料并写完整报告。"}
    trace = long_tool_trace(24)

    # A 24-step trace produces 50 raw protocol messages, but durable chat memory
    # must persist only a user-visible turn rather than delete the task.
    persisted = compact_turn_for_history(user, trace)
    assert len(persisted) == 2, persisted
    assert persisted[0]["role"] == "user"
    assert persisted[1]["role"] == "assistant"
    assert persisted[1]["execution_trace"]["archived"] is True
    assert persisted[1]["execution_trace"]["tool_results"] == 24

    active = [{"role": "system", "content": "You are a task agent."}, user] + trace
    before = estimate_messages_tokens(active)
    compacted, checkpoint = compact_active_agent_context(
        active, model, segment_no=1, reason="test"
    )
    after = estimate_messages_tokens(compacted)
    assert before > after, (before, after)
    assert any(
        item.get("role") == "system" and CHECKPOINT_MARKER in str(item.get("content"))
        for item in compacted
    )
    assert checkpoint["goal"] == user["content"]
    assert checkpoint["completed_tool_results"]

    # Older completed turns may be removed to satisfy budget, but the newly
    # committed protected turn must survive even when old history is huge.
    history: list[dict] = []
    for index in range(8):
        history.extend([
            {"role": "user", "content": f"旧任务 {index} " + ("旧内容 " * 500)},
            {"role": "assistant", "content": "旧答复 " + ("答复 " * 500)},
        ])
    protected_index = len(history)
    history.extend(persisted)
    final_tokens = trim_completed_history_to_budget(
        history, model, protected_from_index=protected_index
    )
    budget = token_budget_for_model(model)
    assert history[-2]["role"] == "user"
    assert history[-2]["content"] == user["content"]
    assert history[-1]["role"] == "assistant"
    assert final_tokens <= budget.input_hard_limit

    print(
        "token context validation passed "
        f"raw_tokens={before} compacted_tokens={after} final_history_tokens={final_tokens}"
    )


if __name__ == "__main__":
    main()
