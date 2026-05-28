"""Validator + Replan signal 단위 테스트."""

from __future__ import annotations

from autonexusgraph.agents.validator import (
    validator_node, should_replan, mark_replan, MAX_REPLANS,
    _extract_big_numbers, _numbers_from_tool_results,
)


def _base_state(answer: str, **overrides) -> dict:
    s = {
        "question": "삼성전자 매출은?",
        "question_kind": "factual",
        "answer": answer,
        "tool_results": [],
        "evidence_chunks": [],
        "n_replans": 0,
        "validation_status": "pending",
    }
    s.update(overrides)
    return s


def test_passes_clean_short_factual():
    """factual + 도구 결과 기반 짧은 답변 — language ok / hallucination none."""
    s = _base_state(
        "삼성전자의 2023년 매출은 258조 9,355억원입니다. [출처: 00126380, 2023]",
        tool_results=[{"tool": "get_revenue", "result": {"value": "258,935,500,000,000"}}],
        evidence_chunks=[{"text": "매출 258,935,500,000,000 원"}],
    )
    out = validator_node(s)
    assert out["validation_status"] == "passed"


def test_fails_when_answer_too_short():
    s = _base_state("OK.")
    out = validator_node(s)
    assert out["validation_status"] == "failed"
    assert "answer_too_short" in out["validation_issues"]


def test_self_reported_insufficient_passes_without_replan():
    """답변이 스스로 '정보 부족' 신고 — replan 의미 없음, passed."""
    s = _base_state("질문에 대한 정보 부족으로 답변할 수 없습니다.")
    out = validator_node(s)
    assert out["validation_status"] == "passed"
    assert out["validation_issues"] == ["self_reported_insufficient"]


def test_hallucinated_number_caught():
    """도구 결과에 없는 큰 숫자가 답변에 등장하면 fail."""
    s = _base_state(
        "삼성전자의 매출은 999,999,999,999원입니다.",
        tool_results=[{"tool": "get_revenue", "result": {"value": "258,935,500,000,000"}}],
        evidence_chunks=[],
    )
    out = validator_node(s)
    assert out["validation_status"] == "failed"
    assert any("hallucinated_numbers" in i for i in out["validation_issues"])


def test_year_4digit_not_treated_as_hallucinated():
    """19xx/20xx 같은 연도는 환각 가드에서 제외."""
    s = _base_state(
        "2023년 매출은 258,935,500,000,000원입니다.",
        tool_results=[{"tool": "get_revenue", "result": "258,935,500,000,000"}],
    )
    out = validator_node(s)
    assert out["validation_status"] == "passed"


def test_language_non_korean_fails():
    s = _base_state(
        "Samsung Electronics revenue in 2023 was over two hundred fifty trillion won, "
        "reflecting strong semiconductor and mobile sales across global markets and regions.",
        tool_results=[],
    )
    out = validator_node(s)
    assert out["validation_status"] == "failed"
    assert any(i.startswith("language_non_korean") for i in out["validation_issues"])


def test_should_replan_respects_max():
    s = _base_state("x", validation_status="failed", n_replans=MAX_REPLANS)
    assert should_replan(s) is False

    s2 = _base_state("x", validation_status="failed", n_replans=0)
    assert should_replan(s2) is True

    s3 = _base_state("x", validation_status="passed", n_replans=0)
    assert should_replan(s3) is False


def test_mark_replan_clears_results_and_increments():
    s = _base_state(
        "fail",
        validation_status="failed",
        n_replans=0,
        plan=[{"tool": "x"}],
        tool_results=[{"tool": "x", "result": "y"}],
        evidence_chunks=[{"text": "z"}],
        citations=[{"chunk_id": 1}],
    )
    out = mark_replan(s)
    assert out["n_replans"] == 1
    assert out["plan"] == []
    assert out["tool_results"] == []
    assert out["evidence_chunks"] == []
    assert out["validation_status"] == "pending"


def test_number_extraction():
    # 콤마 그룹 ≥ 2 → 재무 수치 (백만 이상)
    assert _extract_big_numbers("매출 258,935,500,000,000 원") == {"258935500000000"}
    # 콤마 그룹 1개 (천 단위) — 너무 작으므로 skip
    assert _extract_big_numbers("9,355 만") == set()
    # 4자리 연도 — 식별자 자리수, 재무 수치 아님
    assert _extract_big_numbers("2023년") == set()
    # 8자리 leading-zero 식별자 (corp_code) — skip
    assert _extract_big_numbers("[출처: 00126380]") == set()
    # 7자리 이상 + leading 1-9 → 재무
    assert "9876543210" in _extract_big_numbers("영업이익 9876543210원")
    assert _extract_big_numbers("ROE 9.5%") == set()


def test_numbers_from_tool_results():
    nums = _numbers_from_tool_results([
        {"tool": "get_revenue", "result": {"value": "1,234,567,890"}},
        {"tool": "get_op", "result": "영업이익 9,876,543,210원"},
    ])
    assert "1234567890" in nums
    assert "9876543210" in nums
