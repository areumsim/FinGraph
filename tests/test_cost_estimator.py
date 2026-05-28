"""cost_estimator — Planner 산출 비용 추정 검증."""

from __future__ import annotations

from autonexusgraph.agents.cost_estimator import (
    _approx_tokens,
    estimate_turn_cost,
    needs_cost_approval,
)


def test_approx_tokens_basic():
    assert _approx_tokens("") == 0
    assert _approx_tokens("a") == 1
    # 10 char → 5 tokens
    assert _approx_tokens("abcdefghij") == 5


def _state(**extra):
    s = {
        "question": "삼성전자 매출은?",
        "tool_results": [],
        "evidence_chunks": [],
        "tasks": [],
        "history": [],
        "llm_usage_usd": 0.0,
    }
    s.update(extra)
    return s


def test_estimate_returns_positive_cost():
    est = estimate_turn_cost(_state())
    assert est.estimated_cost_usd >= 0.0
    assert est.expected_input_tokens > 0
    assert est.replan_factor >= 1


def test_estimate_grows_with_more_evidence():
    """evidence chunks 가 많을수록 추정 비용 증가."""
    base = estimate_turn_cost(_state())
    heavy = estimate_turn_cost(_state(
        evidence_chunks=[{"text": "a" * 400} for _ in range(6)],
    ))
    assert heavy.estimated_cost_usd > base.estimated_cost_usd


def test_estimate_grows_with_tool_results():
    base = estimate_turn_cost(_state())
    heavy = estimate_turn_cost(_state(
        tool_results=[{"result": "x" * 1000} for _ in range(5)],
    ))
    assert heavy.estimated_cost_usd >= base.estimated_cost_usd


def test_estimate_includes_replan_factor():
    """replan_factor 가 1 + max_replans 인지."""
    est = estimate_turn_cost(_state())
    assert est.estimated_cost_usd >= est.base_cost_usd * est.replan_factor - 1e-6


def test_needs_cost_approval_below_threshold(monkeypatch):
    """임계 위면 False."""
    monkeypatch.setenv("LLM_COST_AUTO_APPROVE_USD", "100.00")
    from autonexusgraph import config
    config.get_settings.cache_clear()   # type: ignore[attr-defined]
    need, est = needs_cost_approval(_state())
    assert need is False
    assert est.estimated_cost_usd >= 0.0


def test_needs_cost_approval_above_threshold(monkeypatch):
    """임계 아래면 True."""
    monkeypatch.setenv("LLM_COST_AUTO_APPROVE_USD", "0.00001")
    from autonexusgraph import config
    config.get_settings.cache_clear()   # type: ignore[attr-defined]
    need, est = needs_cost_approval(_state())
    assert need is True


def test_format_string_contains_model_and_cost():
    est = estimate_turn_cost(_state())
    s = est.format()
    assert "TURN COST EST" in s
    assert "$" in s
