"""Triage 모호성 검출 + 폴백/재개 흐름 검증."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from autonexusgraph.agents.interrupts import InterruptUnavailable
from autonexusgraph.agents.nodes import triage_node


def _ambiguous_hits(word, limit=5):
    """모호한 회사 후보 — 점수 비등."""
    if word in ("삼성", "samsung"):
        return [
            {"corp_code": "00126380", "name": "삼성전자(주)", "score": 100,
             "stock_code": "005930", "market": "KOSPI"},
            {"corp_code": "00126362", "name": "삼성SDI(주)", "score": 95,
             "stock_code": "006400", "market": "KOSPI"},
            {"corp_code": "00164779", "name": "삼성SDS(주)", "score": 90,
             "stock_code": "018260", "market": "KOSPI"},
        ]
    return []


def _unique_hits(word, limit=5):
    if word == "삼성전자":
        return [{"corp_code": "00126380", "name": "삼성전자", "score": 100}]
    return []


def test_fallback_auto_resolves_ambiguous_with_signal():
    """langgraph interrupt 미사용 환경 — 1순위 자동 선택 + safety_signal."""
    state = {"question": "삼성 매출은?", "llm_usage_usd": 0.0}
    with patch("autonexusgraph.tools.financials.lookup_company", _ambiguous_hits), \
         patch("autonexusgraph.agents.interrupts.request_interrupt",
                side_effect=InterruptUnavailable("test")):
        triage_node(state)
    # 1순위 (삼성전자) 자동 선택
    assert state["target_companies"] == ["00126380"]
    # 경고 signal 등록
    signals = state.get("safety_signals") or []
    assert any("ambiguous_company_auto_resolved" in s for s in signals)
    assert state.get("interrupt_handled") is not True


def test_unique_match_no_interrupt():
    """모호 없으면 interrupt 안 부르고 그냥 1순위."""
    state = {"question": "삼성전자 매출은?", "llm_usage_usd": 0.0}
    with patch("autonexusgraph.tools.financials.lookup_company", _unique_hits) as m_lookup, \
         patch("autonexusgraph.agents.interrupts.request_interrupt") as m_req:
        triage_node(state)
    assert state["target_companies"] == ["00126380"]
    m_req.assert_not_called()


def test_interrupt_response_applied_on_resume():
    """이전 turn 에서 사용자가 응답한 값 — pending_interrupt + interrupt_response 가 있으면 해당 corp_code 채택."""
    state = {
        "question": "삼성 매출은?",
        "pending_interrupt": {
            "kind": "company_clarification",
            "candidates": [
                {"corp_code": "00126380", "name": "삼성전자"},
                {"corp_code": "00126362", "name": "삼성SDI"},
                {"corp_code": "00164779", "name": "삼성SDS"},
            ],
        },
        "interrupt_response": {"index": 2},   # 삼성SDS
        "llm_usage_usd": 0.0,
    }
    # lookup_company 호출 안 일어나도 응답으로 결정돼야 함
    with patch("autonexusgraph.tools.financials.lookup_company", _ambiguous_hits):
        triage_node(state)
    assert state["target_companies"] == ["00164779"]
    assert state.get("interrupt_handled") is True
    assert state.get("pending_interrupt") == {}


def test_interrupt_called_when_ambiguous_and_lg_available():
    """langgraph 활성 시 interrupt 가 호출되고 응답으로 corp_code 채택."""
    state = {"question": "삼성 매출은?", "llm_usage_usd": 0.0}
    with patch("autonexusgraph.tools.financials.lookup_company", _ambiguous_hits), \
         patch("autonexusgraph.agents.interrupts.request_interrupt",
                return_value={"index": 1}) as m_req:
        triage_node(state)
    m_req.assert_called_once()
    assert state["target_companies"] == ["00126362"]   # index=1 (SDI)
    assert state.get("interrupt_handled") is True
    assert state.get("pending_interrupt") == {}


def test_interrupt_invalid_response_does_not_set_target():
    """사용자가 알 수 없는 응답을 보낸 경우 target_companies 비어 있음."""
    state = {"question": "삼성 매출은?", "llm_usage_usd": 0.0}
    with patch("autonexusgraph.tools.financials.lookup_company", _ambiguous_hits), \
         patch("autonexusgraph.agents.interrupts.request_interrupt",
                return_value="garbage"):
        triage_node(state)
    # garbage 응답 → coerce 실패 → target 비어 있음 (다음 word 로 진행하거나 비어진 채 마침)
    assert "00126380" not in (state.get("target_companies") or [])
