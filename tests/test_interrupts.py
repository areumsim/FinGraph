"""HITL interrupt — payload 형식·모호성 검출·응답 해석 검증 (PRD §7.5.6)."""

from __future__ import annotations

import pytest

from autonexusgraph.agents.interrupts import (
    InterruptUnavailable,
    coerce_clarification_response,
    is_ambiguous_company,
    make_clarification_payload,
    request_interrupt,
)


# ── is_ambiguous_company ────────────────────────────────────
def test_empty_or_single_candidate_not_ambiguous():
    assert not is_ambiguous_company([])
    assert not is_ambiguous_company([{"corp_code": "00126380", "score": 100}])


def test_two_candidates_close_score_ambiguous():
    candidates = [
        {"corp_code": "00126380", "name": "삼성전자(주)", "score": 100},
        {"corp_code": "00126362", "name": "삼성SDI", "score": 95},
    ]
    assert is_ambiguous_company(candidates)


def test_two_candidates_far_score_not_ambiguous():
    candidates = [
        {"corp_code": "00126380", "name": "삼성전자", "score": 100},
        {"corp_code": "99999999", "name": "삼성서비스마스터", "score": 30},
    ]
    assert not is_ambiguous_company(candidates)


def test_two_candidates_no_score_treated_as_ambiguous():
    """score 가 없는 환경 — 후보 여럿이면 보수적으로 모호 처리."""
    candidates = [
        {"corp_code": "00126380", "name": "X"},
        {"corp_code": "00126362", "name": "Y"},
    ]
    assert is_ambiguous_company(candidates)


def test_custom_margin():
    candidates = [
        {"score": 100}, {"score": 91},
    ]
    assert is_ambiguous_company(candidates, max_margin=0.10)
    assert not is_ambiguous_company(candidates, max_margin=0.05)


# ── make_clarification_payload ──────────────────────────────
def test_payload_structure():
    cands = [
        {"corp_code": "00126380", "name": "삼성전자", "stock_code": "005930", "market": "KOSPI"},
        {"corp_code": "00126362", "name": "삼성SDI", "stock_code": "006400", "market": "KOSPI"},
    ]
    p = make_clarification_payload("삼성", cands, thread_id="t1")
    assert p["kind"] == "company_clarification"
    assert "삼성" in p["prompt"]
    assert p["thread_id"] == "t1"
    assert len(p["candidates"]) == 2
    assert p["candidates"][0]["corp_code"] == "00126380"


def test_payload_truncates_to_limit():
    cands = [{"corp_code": f"{i:08d}", "name": f"C{i}"} for i in range(10)]
    p = make_clarification_payload("X", cands, limit=3)
    assert len(p["candidates"]) == 3


# ── coerce_clarification_response ───────────────────────────
def test_coerce_by_index():
    cands = [
        {"corp_code": "00126380", "name": "삼성전자"},
        {"corp_code": "00126362", "name": "삼성SDI"},
    ]
    assert coerce_clarification_response(1, cands) == "00126362"
    assert coerce_clarification_response({"index": 0}, cands) == "00126380"


def test_coerce_by_corp_code_dict():
    cands = [{"corp_code": "00126380"}]
    assert coerce_clarification_response({"corp_code": "00126362"}, cands) == "00126362"


def test_coerce_by_direct_corp_code_string():
    cands = [{"corp_code": "00126380"}]
    assert coerce_clarification_response("00164779", cands) == "00164779"


def test_coerce_by_name_match():
    cands = [
        {"corp_code": "00126380", "name": "삼성전자"},
        {"corp_code": "00126362", "name": "삼성SDI"},
    ]
    assert coerce_clarification_response("삼성SDI", cands) == "00126362"


def test_coerce_invalid_returns_none():
    cands = [{"corp_code": "00126380"}]
    assert coerce_clarification_response("invalid", cands) is None
    assert coerce_clarification_response(99, cands) is None
    assert coerce_clarification_response({}, cands) is None
    assert coerce_clarification_response(None, cands) is None


# ── request_interrupt ───────────────────────────────────────
def test_request_interrupt_raises_when_langgraph_missing(monkeypatch):
    """langgraph.types.interrupt import 막아서 fallback 환경 시뮬."""
    import sys
    # langgraph 가 있어도 types.interrupt 만 막기는 어려우니, 호출 자체가 raise 하도록
    # interrupt 함수를 임시로 ImportError 던지게.
    try:
        from langgraph.types import interrupt as _real_interrupt   # noqa: F401
    except ImportError:
        # 이미 미설치 환경 — request_interrupt 가 InterruptUnavailable 던져야
        with pytest.raises(InterruptUnavailable):
            request_interrupt({"kind": "company_clarification", "prompt": "?", "candidates": []})
        return
    # 설치된 환경 — module 자체를 임시 제거
    saved = sys.modules.pop("langgraph.types", None)
    try:
        sys.modules["langgraph.types"] = None   # type: ignore[assignment]
        # 일부 환경에서 langgraph.graph.interrupt 가 별도로 있을 수 있어 그것까지 제거
        saved_graph = sys.modules.pop("langgraph.graph", None)
        sys.modules["langgraph.graph"] = None   # type: ignore[assignment]
        try:
            with pytest.raises(InterruptUnavailable):
                request_interrupt({"kind": "company_clarification", "prompt": "?", "candidates": []})
        finally:
            if saved_graph is not None:
                sys.modules["langgraph.graph"] = saved_graph
    finally:
        if saved is not None:
            sys.modules["langgraph.types"] = saved
