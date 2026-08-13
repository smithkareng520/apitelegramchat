#!/usr/bin/env python3
"""长 Agent 回合上下文压缩的独立回归测试。"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
import apitelegramchat.app as app_module  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _long_turn() -> list[dict]:
    return [
        {"role": "user", "content": "请完成旧任务的资料核验与结论整理。" * 160},
        {
            "role": "assistant",
            "content": "先检索资料，再核对来源。",
            "tool_calls": [{
                "id": "call_search_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query":"资料核验"}'},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_search_1",
            "name": "web_search",
            "content": "原始检索结果" * 500,
        },
        {"role": "assistant", "content": "阶段性结论：已完成资料核验，待继续整合最终建议。" * 100},
    ]


def test_compaction_replaces_entire_tool_turn_with_summary() -> None:
    history = _long_turn() + [
        {"role": "user", "content": "保留中的最新任务"},
        {"role": "assistant", "content": "最新任务已开始。"},
    ]

    compacted = app_module._compact_oldest_history_turn(history)

    require(compacted, "应能压缩最早的完整用户回合")
    require(history[0]["role"] == "system", "压缩内容应成为可安全注入模型的 system 摘要")
    summary = history[0]["content"]
    require(summary.startswith(app_module._CONTEXT_SUMMARY_MARKER), "摘要必须具有稳定的识别标记")
    require("用户目标" in summary and "已完成结论" in summary, "摘要必须保留目标和结果")
    require("web_search" in summary, "摘要必须保留工具轨迹")
    require(not any(msg.get("role") == "tool" for msg in history), "原始工具结果必须与 tool_call 原子组一起移出")
    require(history[-2]["content"] == "保留中的最新任务", "较新的回合必须原样保留")


async def test_preflight_compacts_long_turn_instead_of_discarding_it() -> None:
    chat_id = 918273
    model_name = "__context_compaction_validation__"
    original_model = app_module.SUPPORTED_MODELS.get(model_name)
    original_selected = app_module.user_models.get(chat_id)
    ctx = app_module.get_or_init_context(chat_id)
    original_context = dict(ctx)
    app_module.SUPPORTED_MODELS[model_name] = SimpleNamespace(max_context=4500, max_output_tokens=500)
    app_module.user_models[chat_id] = model_name
    try:
        ctx["conversation_history"] = _long_turn()
        ctx["token_ledger"] = []
        ctx["last_prompt_tokens"] = 0
        ctx["last_completion_tokens"] = 0

        accepted = await app_module.pre_flight_context_check(
            chat_id,
            {"role": "user", "content": "请基于刚才的核验继续给出下一步。"},
        )

        require(accepted, "压缩后应为新的用户输入腾出上下文预算")
        history = ctx["conversation_history"]
        require(len(history) == 1 and history[0]["role"] == "system", "长回合应压缩为一个摘要而非清空历史")
        summary = history[0]["content"]
        require("资料核验" in summary and "web_search" in summary, "摘要必须保留可继续执行的任务线索")
        require(ctx["last_prompt_tokens"] > 0, "压缩后必须同步重建上下文 token 估算")
    finally:
        if original_model is None:
            app_module.SUPPORTED_MODELS.pop(model_name, None)
        else:
            app_module.SUPPORTED_MODELS[model_name] = original_model
        if original_selected is None:
            app_module.user_models.pop(chat_id, None)
        else:
            app_module.user_models[chat_id] = original_selected
        ctx.clear()
        ctx.update(original_context)


def main() -> None:
    test_compaction_replaces_entire_tool_turn_with_summary()
    asyncio.run(test_preflight_compacts_long_turn_instead_of_discarding_it())
    print("context compaction validation: PASS")


if __name__ == "__main__":
    main()
