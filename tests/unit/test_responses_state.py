from __future__ import annotations

import responses_state as rs


VENDOR = "lfree|https://ai.lfree.org/bot/test/v1|openai_responses"
VENDOR_OTHER = "other|https://other.example/v1|openai_responses"
MODEL_A = "model-a"
MODEL_B = "model-b"


# ---------------------------------------------------------------------------
# 单一状态源：chat 只有一个 previous_response_id 链头
# ---------------------------------------------------------------------------
def test_first_turn_bootstraps_then_commit_records_chain_head() -> None:
    st = rs.ResponseState()
    turn = st.begin_turn("USER")

    prev_id, mode = st.resolve_chain(VENDOR, MODEL_A)
    assert prev_id is None
    assert mode == f"bootstrap:{rs.RESPONSE_ID_MISSING}"

    assert st.commit_response(
        turn, vendor_key=VENDOR, response_id="resp_a1", model=MODEL_A,
    )
    ref = st.chain
    assert ref is not None
    assert ref.response_id == "resp_a1"
    assert ref.model == MODEL_A
    assert ref.vendor_key == VENDOR


def test_chain_mode_returns_previous_response_id() -> None:
    st = rs.ResponseState()
    t1 = st.begin_turn("USER")
    st.commit_response(t1, vendor_key=VENDOR, response_id="resp_a1", model=MODEL_A)

    prev_id, mode = st.resolve_chain(VENDOR, MODEL_A)
    assert prev_id == "resp_a1"
    assert mode == "chain"


def test_single_chain_pointer_is_overwritten_per_chat() -> None:
    """同一 chat 只有一个链头：新成功 response 原子覆盖旧值。"""
    st = rs.ResponseState()
    t1 = st.begin_turn("USER")
    st.commit_response(t1, vendor_key=VENDOR, response_id="resp_a1", model=MODEL_A)
    t2 = st.begin_turn("USER")
    st.commit_response(t2, vendor_key=VENDOR, response_id="resp_a2", model=MODEL_A)
    assert st.chain.response_id == "resp_a2"

    prev_id, mode = st.resolve_chain(VENDOR, MODEL_A)
    assert (prev_id, mode) == ("resp_a2", "chain")


# ---------------------------------------------------------------------------
# 异常状态：失败不推进链
# ---------------------------------------------------------------------------
def test_commit_without_response_id_is_rejected() -> None:
    st = rs.ResponseState()
    turn = st.begin_turn("USER")
    assert not st.commit_response(
        turn, vendor_key=VENDOR, response_id=None, model=MODEL_A,
    )
    assert st.chain is None


def test_commit_after_clear_is_rejected_by_generation_fencing() -> None:
    st = rs.ResponseState()
    turn = st.begin_turn("USER")
    st.reset()  # /clear：generation +1，链头清空
    assert not st.commit_response(
        turn, vendor_key=VENDOR, response_id="late_resp", model=MODEL_A,
    )
    assert st.chain is None


def test_invalidate_chain_forces_bootstrap() -> None:
    st = rs.ResponseState()
    t1 = st.begin_turn("USER")
    st.commit_response(t1, vendor_key=VENDOR, response_id="resp_a1", model=MODEL_A)

    st.invalidate_chain("turn_interrupted_midflight")
    prev_id, mode = st.resolve_chain(VENDOR, MODEL_A)
    assert prev_id is None
    assert mode.startswith("bootstrap:")
    assert st.chain.invalid_reason == "turn_interrupted_midflight"


def test_invalidate_chain_is_idempotent_without_chain() -> None:
    st = rs.ResponseState()
    st.invalidate_chain("whatever")
    assert st.chain is not None  # 占位 ref，response_id 为空
    assert st.chain.response_id is None


# ---------------------------------------------------------------------------
# 模型 / 端点切换：断链 + 不复活旧链
# ---------------------------------------------------------------------------
def test_model_switch_breaks_chain_and_bootstraps() -> None:
    st = rs.ResponseState()
    t1 = st.begin_turn("USER")
    st.commit_response(t1, vendor_key=VENDOR, response_id="resp_a1", model=MODEL_A)

    prev_id, mode = st.resolve_chain(VENDOR, MODEL_B)
    assert prev_id is None
    assert mode == f"bootstrap:{rs.MODEL_CHANGED}"
    assert st.chain.response_id is None


def test_switching_back_does_not_resurrect_old_model_chain() -> None:
    st = rs.ResponseState()
    t1 = st.begin_turn("USER")
    st.commit_response(t1, vendor_key=VENDOR, response_id="resp_a1", model=MODEL_A)

    # A → B：断链
    assert st.resolve_chain(VENDOR, MODEL_B)[1].startswith("bootstrap:")
    t2 = st.begin_turn("USER")
    st.commit_response(t2, vendor_key=VENDOR, response_id="resp_b1", model=MODEL_B)

    # B → A：旧 A 链已销毁，仍 bootstrap（B 期间的上下文不在 A 旧链里）
    prev_id, mode = st.resolve_chain(VENDOR, MODEL_A)
    assert prev_id is None
    assert mode.startswith("bootstrap:")


def test_endpoint_partition_change_breaks_chain() -> None:
    st = rs.ResponseState()
    t1 = st.begin_turn("USER")
    st.commit_response(t1, vendor_key=VENDOR, response_id="resp_a1", model=MODEL_A)

    prev_id, mode = st.resolve_chain(VENDOR_OTHER, MODEL_A)
    assert prev_id is None
    assert mode.startswith("bootstrap:")


# ---------------------------------------------------------------------------
# 跨协议写入 / 恢复
# ---------------------------------------------------------------------------
def test_legacy_divergence_invalidates_chain() -> None:
    chat_id = 991000001
    st = rs.get_response_state_sync(chat_id)
    turn = st.begin_turn("USER")
    st.commit_response(turn, vendor_key=VENDOR, response_id="resp_a1", model=MODEL_A)

    rs.mark_legacy_divergence(chat_id)
    prev_id, mode = st.resolve_chain(VENDOR, MODEL_A)
    assert prev_id is None
    assert mode.startswith("bootstrap:")


def test_export_and_restore_chain_state_roundtrip() -> None:
    """规则 9：恢复的是 ID（chat_id / model / previous_response_id），
    不伪造任何历史。"""
    chat_id = 991000002
    st = rs.get_response_state_sync(chat_id)
    turn = st.begin_turn("USER")
    st.commit_response(turn, vendor_key=VENDOR, response_id="resp_keep", model=MODEL_A)

    dumped = rs.export_chain_state()
    entry = dumped[str(chat_id)]
    assert entry["previous_response_id"] == "resp_keep"
    assert entry["model"] == MODEL_A
    assert entry["vendor_key"] == VENDOR

    # 模拟重启：全局注册表清空后按 ID 恢复，链继续。
    rs._response_states.clear()
    assert rs.restore_chain_state(
        chat_id, model=MODEL_A, previous_response_id="resp_keep", vendor_key=VENDOR,
    )
    prev_id, mode = rs.resolve_response_chain(chat_id, VENDOR, MODEL_A)
    assert (prev_id, mode) == ("resp_keep", "chain")

    # 空 response_id 拒绝恢复。
    assert not rs.restore_chain_state(chat_id, model=MODEL_A, previous_response_id="")


def test_turn_registration_and_active_turns() -> None:
    chat_id = 991000003
    st = rs.get_response_state_sync(chat_id)
    turn = st.begin_turn("USER")
    rs.register_active_turn(chat_id, turn)
    assert rs.has_active_turns(chat_id)
    rs.unregister_active_turn(chat_id, turn)
    assert not rs.has_active_turns(chat_id)
