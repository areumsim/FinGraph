"""cost_approval interrupt — planner 통합 + helper 검증."""

from __future__ import annotations

from unittest.mock import patch

from autonexusgraph.agents.interrupts import (
    InterruptUnavailable,
    coerce_cost_response,
    make_cost_approval_payload,
)
from autonexusgraph.agents.nodes import planner_node


# ── helpers ─────────────────────────────────────────────────
def test_payload_structure():
    p = make_cost_approval_payload(
        estimated_cost_usd=0.75,
        plan_summary="multi_hop, 3 회사, 7 task",
        thread_id="t1",
    )
    assert p["kind"] == "cost_approval"
    assert p["estimated_cost_usd"] == 0.75
    assert "$0.75" in p["prompt"] or "0.75" in p["prompt"]
    assert p["thread_id"] == "t1"


def test_coerce_truthy():
    assert coerce_cost_response(True) is True
    assert coerce_cost_response("yes") is True
    assert coerce_cost_response("승인") is True
    assert coerce_cost_response({"approved": True}) is True
    assert coerce_cost_response("OK") is True


def test_coerce_falsy_and_invalid_default_to_false():
    """보수적: 알 수 없는 값은 False (비용 발생 방지)."""
    assert coerce_cost_response(False) is False
    assert coerce_cost_response(None) is False
    assert coerce_cost_response("no") is False
    assert coerce_cost_response("거절") is False
    assert coerce_cost_response({"approved": False}) is False
    assert coerce_cost_response("garbage") is False
    assert coerce_cost_response(123) is False
    assert coerce_cost_response({}) is False


# ── planner 통합 ────────────────────────────────────────────
def _planner_state(**extra):
    s = {
        "question": "삼성전자 자회사 매출",
        "question_kind": "multi_hop",
        "target_companies": ["00126380"],
        "llm_usage_usd": 0.0,
        "n_replans": 0,
    }
    s.update(extra)
    return s


def test_planner_skips_approval_when_below_threshold(monkeypatch):
    """임계가 높으면 cost_approval interrupt 안 부름."""
    monkeypatch.setenv("LLM_COST_AUTO_APPROVE_USD", "100.0")
    from autonexusgraph import config
    config.get_settings.cache_clear()   # type: ignore[attr-defined]
    state = _planner_state()
    with patch("autonexusgraph.agents.interrupts.request_interrupt") as m_req:
        planner_node(state)
    m_req.assert_not_called()
    assert state.get("aborted_reason") is None


def test_planner_calls_interrupt_when_above_threshold(monkeypatch):
    """임계가 낮아 cost approval interrupt 발동 → 승인 시 turn 계속."""
    monkeypatch.setenv("LLM_COST_AUTO_APPROVE_USD", "0.0000001")
    from autonexusgraph import config
    config.get_settings.cache_clear()   # type: ignore[attr-defined]
    state = _planner_state()
    with patch("autonexusgraph.agents.interrupts.request_interrupt",
                return_value=True) as m_req:
        planner_node(state)
    m_req.assert_called_once()
    assert state.get("aborted_reason") is None
    assert state.get("interrupt_handled") is True
    assert state.get("pending_interrupt") == {}


def test_planner_aborts_on_cost_rejection(monkeypatch):
    """사용자가 거절하면 aborted_reason='cost_rejected'."""
    monkeypatch.setenv("LLM_COST_AUTO_APPROVE_USD", "0.0000001")
    from autonexusgraph import config
    config.get_settings.cache_clear()   # type: ignore[attr-defined]
    state = _planner_state()
    with patch("autonexusgraph.agents.interrupts.request_interrupt",
                return_value=False):
        planner_node(state)
    assert state["aborted_reason"] == "cost_rejected"
    assert state.get("interrupt_handled") is True


def test_planner_fallback_on_interrupt_unavailable(monkeypatch):
    """langgraph interrupt 미지원 환경 → 자동 통과 + safety_signal."""
    monkeypatch.setenv("LLM_COST_AUTO_APPROVE_USD", "0.0000001")
    from autonexusgraph import config
    config.get_settings.cache_clear()   # type: ignore[attr-defined]
    state = _planner_state()
    with patch("autonexusgraph.agents.interrupts.request_interrupt",
                side_effect=InterruptUnavailable("test")):
        planner_node(state)
    # 진행 — aborted 안 됨
    assert state.get("aborted_reason") is None
    signals = state.get("safety_signals") or []
    assert any("cost_approval_auto_passed" in s for s in signals)


def test_planner_skips_approval_during_replan(monkeypatch):
    """replan 진행 중에는 사용자에게 또 묻지 않는다 (n_replans>0)."""
    monkeypatch.setenv("LLM_COST_AUTO_APPROVE_USD", "0.0000001")
    from autonexusgraph import config
    config.get_settings.cache_clear()   # type: ignore[attr-defined]
    state = _planner_state(n_replans=1)
    with patch("autonexusgraph.agents.interrupts.request_interrupt") as m_req:
        planner_node(state)
    m_req.assert_not_called()


def test_planner_applies_existing_cost_response(monkeypatch):
    """resume 후 planner 재진입 — pending_interrupt + response 이미 있으면 그것을 적용."""
    monkeypatch.setenv("LLM_COST_AUTO_APPROVE_USD", "0.0000001")
    from autonexusgraph import config
    config.get_settings.cache_clear()   # type: ignore[attr-defined]
    state = _planner_state(
        pending_interrupt={
            "kind": "cost_approval",
            "estimated_cost_usd": 0.75,
        },
        interrupt_response=True,   # 승인
    )
    with patch("autonexusgraph.agents.interrupts.request_interrupt") as m_req:
        planner_node(state)
    # 추가 interrupt 호출 없이 응답 그대로 적용
    m_req.assert_not_called()
    assert state.get("interrupt_handled") is True
    assert state.get("aborted_reason") is None


def test_planner_rejection_response_aborts(monkeypatch):
    monkeypatch.setenv("LLM_COST_AUTO_APPROVE_USD", "0.0000001")
    from autonexusgraph import config
    config.get_settings.cache_clear()   # type: ignore[attr-defined]
    state = _planner_state(
        pending_interrupt={"kind": "cost_approval", "estimated_cost_usd": 0.75},
        interrupt_response=False,
    )
    planner_node(state)
    assert state["aborted_reason"] == "cost_rejected"
