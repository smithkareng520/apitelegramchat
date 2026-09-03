#!/usr/bin/env python3
"""verify_context_strategy.py — 新上下文策略（有界窗口 + 摊销式自动压缩）
的行为验证脚本。

覆盖：
  1. 预算解析 resolve_history_budget（比例 / 输出约束 / 绝对覆盖 / 兜底）；
  2. 结构拆分 split_history_blocks（用户轮锚定 / 前导孤立块 / 摘要槽位）；
  3. 淘汰规划 plan_turn_eviction（目标水位 / 保护尾 / 摘要永不淘汰）；
  4. 滚动摘要 build_digest_text（确定性 / 合并 / 预算截断 / 归档指针 /
     轮数累计 / 多模态占位）；
  5. 请求守卫 select_request_context（快路径全量透传 + 浅拷贝隔离 /
     兜底按块淘汰 / 孤儿 tool 清理 / 单消息超预算截断）；
  6. 多轮模拟：**前缀字节稳定性**——请求前缀只在压缩事件轮变化，
     其余轮逐字节一致（隐式前缀缓存全量命中的前提）；事件不连发
     （无抖动）；守卫输出永远不超过预算。

运行：python3 scripts/verify_context_strategy.py（依赖 tiktoken，
已在 requirements.txt 中）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apitelegramchat.context_window import (  # noqa: E402
    DIGEST_MARKER,
    apply_eviction_plan,
    build_digest_text,
    compact_watermarks,
    count_history_tokens,
    effective_digest_budget,
    is_digest_message,
    make_digest_message,
    plan_turn_eviction,
    resolve_history_budget,
    split_history_blocks,
)
from apitelegramchat.context_manager import select_request_context  # noqa: E402
from apitelegramchat.token_budget import json_token_count  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def asst(text: str) -> dict:
    return {"role": "assistant", "content": text}


def asst_tool(name: str, args: dict, call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }],
    }


def tool(call_id: str, text: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def make_turn(i: int, *, with_tool: bool = False) -> list[dict]:
    """一轮对话（user + 可选工具对 + assistant 结论）。"""
    msgs = [user(f"第{i}轮：请帮我分析这个方案的可行性，重点看成本与风险，"
                 f"并结合最近的行业动态给出建议，谢谢。")]
    if with_tool:
        msgs.append(asst_tool(
            "fetch_url", {"url": f"https://example.com/report-{i}"}, f"call_{i}",
        ))
        msgs.append(tool(f"call_{i}", "Tool result archived at "
                                      f".context-archive/tool-results/call-{i}-abc.json. "
                                      "Use text_editor view to retrieve."))
    msgs.append(asst(f"第{i}轮结论：总体可行，但需要控制预算并分两期实施；"
                     f"主要风险在供应链与合规侧，建议预留 15% 缓冲。"))
    return msgs


# ---------------------------------------------------------------------------
print("== 1. resolve_history_budget ==")
check("128k 窗口 × 0.8 = 102400", resolve_history_budget(128000, 8192) == 102400)
check("输出约束更紧时取 min（128k/64k → 62464）", resolve_history_budget(128000, 65536) == 62464)
check("无模型信息兜底 50000", resolve_history_budget() == 50000)
check("绝对覆盖优先", resolve_history_budget(128000, 8192, absolute_override=30000) == 30000)
check("小窗口下限 1024", resolve_history_budget(2000, 1900) == 1024)
check("水位：trigger=0.9×budget, target=0.5×budget",
      compact_watermarks(10000) == (9000, 5000))
check("摘要预算不超过 budget/4", effective_digest_budget(3000) == 750
      and effective_digest_budget(100000) == 1500)

# ---------------------------------------------------------------------------
print("== 2. split_history_blocks ==")
digest = make_digest_message(DIGEST_MARKER + " 早期 3 轮…\n- U: a")
history = [digest] + [asst("孤立头"), tool("x", "r")] + [
    user("u1"), asst("a1"),
    user("u2"), asst_tool("wikipedia", {"query": "q"}, "c2"), tool("c2", "ok"),
    {"role": "system", "content": "回合内通知"}, user("u3"), asst("a3"),
]
d, blocks = split_history_blocks(history)
check("摘要槽位单独抽出", d is digest)
check("前导孤立块（无 user 锚点）成块", blocks[0] == [asst("孤立头"), tool("x", "r")])
check("user 开启新块", blocks[1][0] is history[3] and len(blocks[1]) == 2)
check("轮内 tool 与紧随的 system 通知归属 user 块", len(blocks[2]) == 4)
check("下一条 user 仍开启新块", blocks[3][0]["role"] == "user" and len(blocks[3]) == 2)
check("块数正确", len(blocks) == 4)

# ---------------------------------------------------------------------------
print("== 3. plan_turn_eviction ==")
turns = [make_turn(i, with_tool=(i % 2 == 0)) for i in range(1, 11)]
flat = [m for t in turns for m in t]
total = count_history_tokens(flat)
plan = plan_turn_eviction(flat, target_tokens=total, protected_turns=6)
check("预算内 no-op", plan.evicted_blocks == [] and plan.kept_messages == flat)

plan = plan_turn_eviction(flat, target_tokens=total // 2, protected_turns=6)
check("淘汰从最老整轮开始", plan.evicted_blocks and plan.evicted_blocks[0] == turns[0])
check("淘汰量把总量压到目标附近", total - plan.evicted_tokens <= total // 2 + 400,
      f"evicted={plan.evicted_tokens} total={total}")
check("保护尾：剩余轮数 ≥ 6", len(plan.kept_blocks) >= 6)
check("淘汰是前缀（保序）", [m for b in plan.evicted_blocks for m in b] == flat[:plan.evicted_message_count])

digest2 = make_digest_message(build_digest_text(None, turns[:2], budget_tokens=1500))
flat2 = [digest2] + flat
plan2 = plan_turn_eviction(flat2, target_tokens=0, protected_turns=0)
check("摘要槽位单独抽出且不被计入淘汰块", plan2.digest_message is digest2
      and digest2 not in [m for b in plan2.evicted_blocks for m in b])
flat2_backup = list(flat2)
apply_eviction_plan(flat2, plan2, build_digest_text(digest2["content"], turns[2:4], budget_tokens=1500))
check("落盘后摘要仍居历史头部（永不淘汰）", is_digest_message(flat2[0])
      and len(flat2) < len(flat2_backup))

big = [user("x" * 20000)]
plan3 = plan_turn_eviction(big, target_tokens=10, protected_turns=0)
check("单块超大也按块淘汰（不抛异常）", len(plan3.evicted_blocks) == 1)

# ---------------------------------------------------------------------------
print("== 4. build_digest_text ==")
t1, t2, t3 = make_turn(1), make_turn(2, with_tool=True), make_turn(3)
d1 = build_digest_text(None, [t1], budget_tokens=1500)
d1b = build_digest_text(None, [t1], budget_tokens=1500)
check("确定性：同输入同字节", d1 == d1b)
check("头部含标记与轮数", d1.startswith(DIGEST_MARKER) and "1 轮" in d1.splitlines()[0])
check("U/A 骨架行存在", any(l.startswith("- U:") for l in d1.splitlines())
      and any(l.startswith("  A:") for l in d1.splitlines()))

d2 = build_digest_text(d1, [t2], budget_tokens=1500)
check("合并：旧行保留、轮数累计", d2.startswith(DIGEST_MARKER)
      and "2 轮" in d2.splitlines()[0]
      and "第1轮" in d2 and "第2轮" in d2)
check("归档指针进入 T 行", "已归档 .context-archive/tool-results/call-2-abc.json" in d2)

d3 = build_digest_text(d2, [t3], budget_tokens=120)
check("预算超限从最老行丢弃并标注", "(更早轮次已因摘要" in d3 and "第1轮" not in d3)

multi = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]},
         asst("收到图了")]
dm = build_digest_text(None, [multi], budget_tokens=1500)
check("多模态 user 内容有占位", "[多模态消息" in dm)

# ---------------------------------------------------------------------------
print("== 5. select_request_context（守卫） ==")
hist = [digest2] + flat
snap = select_request_context(hist, max_tokens=1_000_000)
check("快路径：全量透传", snap.messages == hist and snap.dropped_messages == 0)
snap.messages[1]["content"] = "MUTATED"
check("浅拷贝隔离：改出站不影响持久历史", hist[1]["content"] != "MUTATED")

small_digest = make_digest_message(build_digest_text(None, turns[:1], budget_tokens=200))
hist_small = [small_digest] + flat
guard_budget = json_token_count(small_digest) + count_history_tokens(flat[-3:]) + 60
tiny = select_request_context(hist_small, max_tokens=guard_budget)
check("兜底：出站视图不超过预算",
      sum(json_token_count(m) for m in tiny.messages) <= guard_budget)
check("兜底：淘汰发生且摘要槽位保留在头部（预算允许时）",
      len(tiny.messages) < len(hist_small) and is_digest_message(tiny.messages[0]))
check("兜底：不以孤儿 tool 开头",
      not (tiny.messages and tiny.messages[0].get("role") == "tool"))

degenerate = select_request_context(hist_small, max_tokens=220)
check("极端小预算：仍产出合法且不超预算、保留最新轮的视图",
      degenerate.messages and sum(json_token_count(m) for m in degenerate.messages) <= 220
      and any("第10轮" in str(m.get("content")) for m in degenerate.messages
              if m.get("role") == "user"))

orphan = [tool("c0", "x"), user("u"), asst("a")]
snap_o = select_request_context(orphan, max_tokens=100000)
check("孤儿 tool 首条剔除", snap_o.messages[0]["role"] == "user")

huge = [user("字" * 5000)]
snap_h = select_request_context(huge, max_tokens=200)
check("单消息超预算被截断且不超预算",
      len(snap_h.messages) == 1 and json_token_count(snap_h.messages[0]) <= 200
      and snap_h.messages[0]["content"].endswith("…"))

snap_m = select_request_context(flat, model_max_context=128000, model_max_output=65536)
check("model_max_output 参与预算（128k/64k → 62464）仍全量放行",
      len(snap_m.messages) == len(flat))

# ---------------------------------------------------------------------------
print("== 6. 待淘汰区归档下标口径（app.py L2 前置步骤的纯逻辑等价验证） ==")
from apitelegramchat.tool_context_compaction import _eligible_calls  # noqa: E402

turns_r = [make_turn(i, with_tool=True) for i in range(1, 11)]
# 归档候选必须是"未归档的原始 payload"（已是指针的调用会被跳过），
# 与真实会话中待归档区的一致。
for i in range(3, 11):
    turns_r[i - 1][2]["content"] = f"<html>report {i} full payload …</html>"
hist_r = [make_digest_message(build_digest_text(None, turns_r[:2], budget_tokens=1500))]
hist_r += [m for t in turns_r[2:] for m in t]
plan_r = plan_turn_eviction(hist_r, target_tokens=200, protected_turns=6)
digest_off = 1 if plan_r.digest_message is not None else 0
region_lo, region_hi = digest_off, digest_off + plan_r.evicted_message_count
region_ids = {
    idx for idx, _tc, _tr in _eligible_calls(hist_r) if region_lo <= idx < region_hi
}
evicted_msgs = [m for b in plan_r.evicted_blocks for m in b]
evicted_call_ids = {
    tc["id"] for m in evicted_msgs if m.get("role") == "assistant"
    for tc in (m.get("tool_calls") or [])
}
check("待淘汰区下标口径与块内 tool_call 集合一致（含摘要偏移）",
      len(region_ids) == len(evicted_call_ids) and region_ids,
      f"region={sorted(region_ids)} calls={sorted(evicted_call_ids)}")
# 精确校验：最老 len(region_ids) 个 eligible 调用恰为区域内调用
all_eligible = [idx for idx, _tc, _tr in _eligible_calls(hist_r)]
check("最老 N 个 eligible 调用 = 待淘汰区调用（calls_to_compact=N 语义正确）",
      set(all_eligible[:len(region_ids)]) == region_ids,
      f"oldest={all_eligible[:len(region_ids)]} region={sorted(region_ids)}")

# ---------------------------------------------------------------------------
print("== 7. 多轮模拟：前缀稳定性 + 压缩事件 ==")
BUDGET = 5000
TRIGGER, TARGET = compact_watermarks(BUDGET)
DIGEST_BUDGET = effective_digest_budget(BUDGET)
PROTECTED = 6

persisted: list[dict] = []
prev_request: list[dict] | None = None
events = 0
event_rounds: list[int] = []
stable_rounds = 0
prefix_changes = 0

for i in range(1, 41):
    new_msgs = make_turn(i, with_tool=(i % 3 == 0))
    new_user = new_msgs[0]
    new_input_est = json_token_count(new_user)

    # —— pre_flight 决策（纯 L2 确定性模拟，L1 归档为异步 IO 不在此验证）——
    history_est = count_history_tokens(persisted)
    if history_est + new_input_est > TRIGGER:
        events += 1
        event_rounds.append(i)
        plan = plan_turn_eviction(
            persisted,
            target_tokens=max(0, TARGET - new_input_est),
            protected_turns=PROTECTED,
        )
        if plan.evicted_blocks:
            prev_text = plan.digest_message.get("content") if plan.digest_message else None
            digest_text = build_digest_text(prev_text, plan.evicted_blocks,
                                            budget_tokens=DIGEST_BUDGET)
            apply_eviction_plan(persisted, plan, digest_text)

    # —— 请求构建（守卫）——
    snap = select_request_context(persisted, max_tokens=BUDGET)
    request_history = snap.messages
    check_budget = sum(json_token_count(m) for m in request_history)
    assert check_budget <= BUDGET, f"守卫超预算 round={i}"

    if prev_request is not None:
        if request_history[:len(prev_request)] == prev_request:
            stable_rounds += 1
        else:
            prefix_changes += 1
    prev_request = request_history

    # —— 回合完成：写入持久历史（尾部追加）——
    persisted.extend(new_msgs)

check("模拟发生压缩事件（1-3 次）", 1 <= events <= 3, f"events={events}")
check("每次前缀变化都对应压缩事件（无滑动漂移）", prefix_changes == events,
      f"changes={prefix_changes} events={events}")
check("事件轮之外的轮次前缀逐字节稳定", stable_rounds == 40 - 1 - events,
      f"stable={stable_rounds}")
check("事件不连发（无抖动）", all(b - a > 1 for a, b in zip(event_rounds, event_rounds[1:])),
      f"event_rounds={event_rounds}")
check("压缩后历史头部是摘要槽位", is_digest_message(persisted[0]))
check("摘要受预算约束（头部计入）", json_token_count(persisted[0]) <= DIGEST_BUDGET + 48)
check("早期轮要么在摘要中、要么有预算省略标注",
      "第1轮" in persisted[0]["content"] or "更早轮次已因摘要" in persisted[0]["content"])
check("最近轮原文保留", any("第40轮" in str(m.get("content")) for m in persisted[-3:]))
check("持久历史回到目标水位附近",
      count_history_tokens(persisted) <= TARGET + 2 * json_token_count(persisted[0]) + 600)

# ---------------------------------------------------------------------------
print()
print(f"共 {PASS + FAIL} 项断言：通过 {PASS}，失败 {FAIL}")
sys.exit(1 if FAIL else 0)
