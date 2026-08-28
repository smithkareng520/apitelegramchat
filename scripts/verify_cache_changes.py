"""缓存优化验证脚本：对本次全部改动做行为级断言。

运行：python3 scripts/verify_cache_changes.py
覆盖：
  1. _apply_cache_control 三断点策略（system / 历史末尾 / 当前 user 消息）
  2. _merged_extra_body / _openrouter_extra_body（session_id + 顶层 cache_control）
  3. select_request_context 批量淘汰（窗口起点跨轮稳定）
  4. web_search 结果 TTL 缓存（重复查询不再打上游）
  5. R2 预签名 URL 记忆化（同一 key 两次签名返回同一 URL）
  6. _extract_cache_usage 各家 usage 字段解析
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def block_has_marker(msg) -> bool:
    c = msg.get("content")
    return (
        isinstance(c, list)
        and bool(c)
        and isinstance(c[-1], dict)
        and "cache_control" in c[-1]
    )


def test_apply_cache_control():
    print("== 1. _apply_cache_control 三断点策略 ==")
    from apitelegramchat.ai.attachment_content import _apply_cache_control

    # 场景 A：常规多轮 [system, u1, a1(tool), t1, a1f, u2(新)]
    msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "assistant", "content": "final answer"},
        {"role": "user", "content": "q2"},
    ]
    _apply_cache_control(msgs)
    check("system 断点", block_has_marker(msgs[0]))
    check("上一轮末尾(a1f) 断点", block_has_marker(msgs[4]), f"content={msgs[4].get('content')}")
    check("本轮新 user(u2) 断点", block_has_marker(msgs[5]))
    check("tool 消息不打标记（避免网关兼容问题）", not block_has_marker(msgs[3]))

    # 总断点数 ≤ 3（不含顶层自动缓存的 1 个额度，Anthropic 上限 4）
    total = sum(1 for m in msgs if block_has_marker(m))
    check("显式断点总数 ≤ 3", total <= 3, f"total={total}")

    # 场景 B：空历史首轮 [system, u1]
    msgs2 = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hello"},
    ]
    _apply_cache_control(msgs2)
    check("首轮 system 断点", block_has_marker(msgs2[0]))
    check("首轮 user 断点", block_has_marker(msgs2[1]))

    # 场景 C：幂等重跑（已标记消息不重复计数、不报错）
    _apply_cache_control(msgs)
    total2 = sum(1 for m in msgs if block_has_marker(m))
    check("重跑幂等（断点数不增长）", total2 == total, f"{total2} vs {total}")

    # 场景 D：末尾是 tool 消息（被中断的轮次）——回退到最近的 user/assistant
    msgs3 = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "ans"},
        {"role": "tool", "tool_call_id": "c9", "content": "late tool"},
    ]
    _apply_cache_control(msgs3)
    check("末尾为 tool 时回退标记 assistant", block_has_marker(msgs3[2]))

    # 场景 E：多模态 content（list）末块打标
    msgs4 = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://x/1.png"}},
            {"type": "text", "text": "看图"},
        ]},
    ]
    _apply_cache_control(msgs4)
    check("多模态消息末块打标", block_has_marker(msgs4[1]))
    check("多模态首块不打标", "cache_control" not in msgs4[1]["content"][0])


def test_extra_body():
    print("== 2. OpenRouter session_id + 顶层自动缓存 ==")
    from apitelegramchat.ai.agentic_loops import (
        _merged_extra_body,
        _openrouter_session_id,
    )

    # openrouter + prompt cache 模型
    body = _merged_extra_body("openrouter", {"reasoning": {"enabled": True}}, chat_id=12345, supports_prompt_cache=True)
    check("session_id 存在", body.get("session_id") == "tg-chat-12345", str(body))
    check("顶层 cache_control 存在", body.get("cache_control") == {"type": "ephemeral"})
    check("provider 偏好保留", "provider" in body)
    check("reasoning 字段保留", body.get("reasoning") == {"enabled": True})

    # openrouter + 非 prompt cache 模型（如 DeepSeek 隐式缓存）→ 只有 session_id
    body2 = _merged_extra_body("openrouter", None, chat_id=1, supports_prompt_cache=False)
    check("隐式缓存模型也有 session_id", body2.get("session_id") == "tg-chat-1")
    check("隐式缓存模型不带顶层 cache_control", "cache_control" not in body2)

    # 非 openrouter 厂商 → 不注入任何字段
    body3 = _merged_extra_body("glm", {"thinking": {"type": "enabled"}}, chat_id=1, supports_prompt_cache=True)
    check("非 OpenRouter 不注入 session_id", "session_id" not in (body3 or {}))
    check("非 OpenRouter 不注入 cache_control", "cache_control" not in (body3 or {}))

    # chat_id 为 None → 无 session_id 但不报错
    body4 = _merged_extra_body("openrouter", None, chat_id=None)
    check("chat_id=None 时无 session_id", "session_id" not in body4)

    # 长度限制
    check("session_id ≤ 256 字符", len(_openrouter_session_id("x" * 500)) <= 256)


def test_context_batch_eviction():
    print("== 3. select_request_context 量化淘汰 ==")
    from apitelegramchat.context_manager import select_request_context

    def mk(i: int):
        return {"role": "user" if i % 2 == 0 else "assistant", "content": f"message-{i}-" + "x" * 40}

    # 旧版行为对照：51 条历史、max=50 时每轮滑 1 条（起点逐轮变化）。
    # 新版：淘汰量量化到 step=10 的倍数 → H=51 时一次跳到起点 10。
    history = [mk(i) for i in range(51)]
    snap = select_request_context(history, max_messages=50, max_tokens=100000)
    check("量化后窗口 = 41 条（起点 10）", len(snap.messages) == 41, f"len={len(snap.messages)}")
    check("窗口起点为 message-10", snap.messages[0]["content"].startswith("message-10-"),
          f"head={snap.messages[0]['content'][:12]}")

    # 关键：历史继续增长（每轮 +1 条），起点在 step 轮内保持不变
    head_before = snap.messages[0]["content"]
    for extra in range(1, 10):
        h2 = [mk(i) for i in range(51 + extra)]
        s2 = select_request_context(h2, max_messages=50, max_tokens=100000)
        if s2.messages[0]["content"] != head_before:
            check(f"窗口起点在第 {extra} 轮保持稳定", False, f"head changed: {s2.messages[0]['content'][:12]}")
            return
    check("窗口起点连续 9 轮保持稳定（前缀缓存可命中）", True)

    # 跨过一个 step 后起点阶梯前进 10
    h3 = [mk(i) for i in range(61)]
    s3 = select_request_context(h3, max_messages=50, max_tokens=100000)
    check("H=61 时起点阶梯前进到 message-20", s3.messages[0]["content"].startswith("message-20-"),
          f"head={s3.messages[0]['content'][:12]}")

    # 未触顶的短历史：不做任何淘汰
    short = [mk(i) for i in range(20)]
    s4 = select_request_context(short, max_messages=50, max_tokens=100000)
    check("短历史不触发淘汰", len(s4.messages) == 20, f"len={len(s4.messages)}")

    # step=1 恢复旧行为（恰好 50 条）
    import apitelegramchat.context_manager as cm
    old = cm.EVICT_HEADROOM_MESSAGES
    try:
        cm.EVICT_HEADROOM_MESSAGES = 1
        s5 = select_request_context([mk(i) for i in range(51)], max_messages=50, max_tokens=100000)
        check("step=1 恢复旧行为（恰好 50 条）", len(s5.messages) == 50, f"len={len(s5.messages)}")
    finally:
        cm.EVICT_HEADROOM_MESSAGES = old

    # 首条为 tool 的窗口仍会被剔除（保留原语义）
    hist_tool = [{"role": "tool", "tool_call_id": "c", "content": "r"}] + [mk(i) for i in range(45)]
    s6 = select_request_context(hist_tool, max_messages=50, max_tokens=100000)
    check("孤立 tool 首条仍被剔除", s6.messages[0].get("role") != "tool")

    # token 上限同样走量化：固定小预算下，起点稳定若干轮
    small_msgs = [{"role": "user", "content": f"m{i}-" + "y" * 300} for i in range(200)]
    st = select_request_context(small_msgs, max_messages=1000, max_tokens=3000)
    head_tok = st.messages[0]["content"]
    stable_rounds = 0
    for extra in range(1, 30):
        st2 = select_request_context(small_msgs + [{"role": "user", "content": f"new-{extra}"}],
                                     max_messages=1000, max_tokens=3000)
        if st2.messages[0]["content"] == head_tok:
            stable_rounds += 1
    check("token 触顶时起点仍有连续稳定轮（量化生效）", stable_rounds >= 3,
          f"stable_rounds={stable_rounds}")


def test_search_cache():
    print("== 4. web_search 结果缓存 ==")
    from apitelegramchat import search_engine as se

    calls = {"n": 0}

    async def fake_uncached(**kwargs):
        calls["n"] += 1
        return f"RESULT#{calls['n']} for {kwargs.get('query')}"

    async def run():
        import apitelegramchat.search_engine as m
        m._search_cache.clear()
        m._execute_web_search_uncached = fake_uncached
        # 第一次：miss → 打上游
        r1 = await m.execute_web_search(query="hello world", num_results=5)
        # 第二次（同参数）：hit → 不打上游
        r2 = await m.execute_web_search(query="hello world", num_results=5)
        # num_results 归一化等价（5 与 5 相同；50 是上限钳制值）
        r3 = await m.execute_web_search(query="hello world", num_results=500)  # → 50
        # 不同 query：miss
        r4 = await m.execute_web_search(query="another query")
        return r1, r2, r3, r4

    r1, r2, r3, r4 = asyncio.run(run())
    check("首次查询打上游", calls["n"] >= 1)
    check("重复查询命中缓存（结果相同）", r1 == r2, f"{r1!r} vs {r2!r}")
    # r1 命中 1 次；r2 走缓存；r3（num 归一化为 50，不同 key）与 r4（不同 query）各 miss 1 次
    check("重复查询不打上游（仅 3 次真实调用：r1/r3/r4）", calls["n"] == 3, f"upstream calls={calls['n']}")
    check("不同参数不复用缓存", r4 != r1)

    # 错误结果不缓存
    async def fake_error(**kwargs):
        calls["n"] += 1
        return "❌ 网页搜索服务的上游鉴权失败（HTTP 401）。"

    async def run_err():
        import apitelegramchat.search_engine as m
        m._search_cache.clear()
        m._execute_web_search_uncached = fake_error
        await m.execute_web_search(query="err test")
        await m.execute_web_search(query="err test")
        return calls["n"]

    before = calls["n"]
    asyncio.run(run_err())
    check("服务错误不缓存（每次都重试）", calls["n"] - before == 2, f"delta={calls['n'] - before}")

    # 空结果缓存判定
    check("空结果视为可缓存", se._is_cacheable_search_result("❌ 未找到与「x」相关的结果。"))
    check("服务错误视为不可缓存", not se._is_cacheable_search_result("❌ 网页搜索服务暂未返回有效结果；请稍后重试。"))
    check("成功结果可缓存", se._is_cacheable_search_result("🔍 [成功] ..."))


def test_presign_memo():
    print("== 5. R2 预签名 URL 记忆化 ==")
    from apitelegramchat import s3_utils as s3

    signs = {"n": 0}

    class FakeS3:
        async def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
            signs["n"] += 1
            return f"https://r2.example/bucket/{Params['Key']}?sig=SIGN{signs['n']}"

    class FakeCtx:
        async def __aenter__(self):
            return FakeS3()
        async def __aexit__(self, *a):
            return False

    async def run():
        import apitelegramchat.s3_utils as s3m
        s3m._presigned_url_cache.clear()
        orig_session = s3m.session
        orig_is_configured = s3m.is_r2_configured
        s3m.session = type("S", (), {"client": staticmethod(lambda *a, **k: FakeCtx())})()
        s3m.is_r2_configured = lambda: True
        try:
            u1 = await s3m.generate_presigned_url("telegram/fileA")
            u2 = await s3m.generate_presigned_url("telegram/fileA")
            u3 = await s3m.generate_presigned_url("telegram/fileB")
            # 自定义 expiry 不走记忆化
            u4 = await s3m.generate_presigned_url("telegram/fileA", expires_in=1800)
            return u1, u2, u3, u4
        finally:
            s3m.session = orig_session
            s3m.is_r2_configured = orig_is_configured

    u1, u2, u3, u4 = asyncio.run(run())
    check("同 key 两次调用返回同一 URL（前缀字节稳定）", u1 == u2, f"{u1} vs {u2}")
    check("不同 key 各自签名", u3 != u1)
    check("实际签名次数 = 3（A、B、custom）", signs["n"] == 3, f"signs={signs['n']}")
    check("自定义 expiry 不复用缓存", "sig=SIGN" in u4 and u4 != u1)


def test_cache_usage_extraction():
    print("== 6. 缓存命中观测字段 ==")
    from apitelegramchat.ai.agentic_loops import _extract_cache_usage

    class Details:
        cached_tokens = 9500

    class UsageOpenAI:
        prompt_tokens = 10000
        prompt_tokens_details = Details()

    stats = _extract_cache_usage(UsageOpenAI())
    check("OpenAI/OpenRouter cached_tokens 解析", stats.get("cached") == 9500, str(stats))
    check("命中率计算", stats.get("hit_ratio") == 0.95, str(stats))

    class UsageAnthropic:
        prompt_tokens = 10000
        model_extra = {
            "cache_read_input_tokens": 8000,
            "cache_creation_input_tokens": 1500,
        }

    stats2 = _extract_cache_usage(UsageAnthropic())
    check("Anthropic cache_read 解析", stats2.get("cached") == 8000, str(stats2))
    check("Anthropic cache_write 解析", stats2.get("cache_write") == 1500, str(stats2))

    stats3 = _extract_cache_usage({"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 60}})
    check("dict 形态 usage 解析", stats3.get("cached") == 60 and stats3.get("hit_ratio") == 0.6, str(stats3))

    stats4 = _extract_cache_usage({"prompt_tokens": 100, "prompt_cache_hit_tokens": 50})
    check("DeepSeek prompt_cache_hit_tokens 解析", stats4.get("cached") == 50, str(stats4))

    check("None usage 返回空", _extract_cache_usage(None) == {})
    check("无缓存字段返回空", _extract_cache_usage({"prompt_tokens": 100}) == {"prompt_tokens": 100})


def main():
    test_apply_cache_control()
    test_extra_body()
    test_context_batch_eviction()
    test_search_cache()
    test_presign_memo()
    test_cache_usage_extraction()
    print()
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
