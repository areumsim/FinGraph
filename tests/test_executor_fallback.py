"""Executor fallback recovery — 빈 결과일 때 search_documents 자동 호출."""

from __future__ import annotations

from unittest.mock import patch

from autonexusgraph.agents.nodes import executor_node


def _state(plan: list[dict], **extra) -> dict:
    s = {
        "question": "삼성전자 자회사는?",
        "question_kind": "structural",
        "target_companies": ["00126380"],
        "plan": plan,
        "llm_usage_usd": 0.0,
    }
    s.update(extra)
    return s


def test_fallback_search_when_all_empty():
    """모든 도구가 빈 결과 → search_documents fallback 자동 호출."""
    plan = [
        {"tool": "list_subsidiaries", "args": {"parent_corp_code": "00126380"}, "purpose": "x"},
    ]

    def fake_subs(**kwargs):
        return []

    def fake_search(**kwargs):
        return [
            {"id": "c1", "corp_code": "00126380", "fiscal_year": 2024,
             "text": "본문...", "score": 0.9, "section": "사업"}
        ]

    import autonexusgraph.tools as toolbox
    with patch.object(toolbox, "list_subsidiaries", fake_subs, create=True), \
         patch.object(toolbox, "search_documents", fake_search, create=True):
        out = executor_node(_state(plan))
    assert out.get("fallback_used") is True
    # tool_results 에 fallback 의 search_documents 추가됐는지
    assert any(r["tool"] == "search_documents" for r in out["tool_results"])
    assert out["evidence_chunks"]


def test_no_fallback_when_search_already_in_plan():
    """이미 search_documents 가 plan 에 있으면 fallback 안 함."""
    plan = [
        {"tool": "search_documents", "args": {"query": "X"}, "purpose": "x"},
    ]
    import autonexusgraph.tools as toolbox
    with patch.object(toolbox, "search_documents", lambda **k: [], create=True):
        out = executor_node(_state(plan))
    assert out.get("fallback_used") is not True
    # 빈 결과지만 fallback 도 search 라서 안 부름
    assert sum(1 for r in out["tool_results"] if r["tool"] == "search_documents") == 1


def test_no_fallback_when_results_present():
    """도구가 결과를 냈으면 fallback 불필요."""
    plan = [
        {"tool": "list_subsidiaries", "args": {"parent_corp_code": "00126380"},
         "purpose": "x"},
    ]
    import autonexusgraph.tools as toolbox
    with patch.object(
        toolbox, "list_subsidiaries",
        lambda **k: [{"child_name": "삼성디스플레이"}],
        create=True,
    ):
        out = executor_node(_state(plan))
    assert out.get("fallback_used") is not True


def test_no_fallback_when_budget_exceeded():
    """예산 초과 상황에서는 fallback 도 안 함."""
    # 큰 사용량 미리 적재
    plan = [
        {"tool": "list_subsidiaries", "args": {"parent_corp_code": "00126380"},
         "purpose": "x"},
    ]
    import autonexusgraph.tools as toolbox

    # 빈 결과 + 예산 초과 → break 후 fallback 우회
    with patch.object(toolbox, "list_subsidiaries", lambda **k: [], create=True), \
         patch("autonexusgraph.agents.nodes.turn_budget_exceeded", lambda s: True):
        out = executor_node(_state(plan))
    assert out.get("aborted_reason") == "turn_budget"
    assert out.get("fallback_used") is not True
