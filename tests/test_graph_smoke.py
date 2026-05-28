"""graph.run_agent 폴백 체인 smoke test — langgraph 미설치 환경에서도 동작 검증.

triage/planner/executor/synthesizer/validator 모두 mock 해서 LLM·DB 의존 없이 검증.
"""

from __future__ import annotations

from unittest.mock import patch

import autonexusgraph.agents.graph as g
from autonexusgraph.agents.graph import run_agent


def test_runtime_branch_is_either_langgraph_or_fallback():
    """`_HAS_LANGGRAPH` 플래그가 import 가능 여부와 일치."""
    try:
        import langgraph  # noqa: F401
        assert g._HAS_LANGGRAPH is True
    except ImportError:
        assert g._HAS_LANGGRAPH is False


def test_run_agent_clean_path():
    """node 4개 + validator 모두 패스 → 답변 그대로 반환, n_replans=0."""
    def triage(s):
        s["question_kind"] = "factual"
        s["target_companies"] = ["00126380"]
        return s

    def planner(s):
        s["plan"] = [{"tool": "x", "purpose": "y"}]
        return s

    def executor(s):
        s["tool_results"] = [{"tool": "x", "result": "OK"}]
        s["evidence_chunks"] = [{"text": "본문 내용 다수 토큰 매칭"}]
        return s

    def synth(s):
        s["answer"] = "정상 답변입니다. 본문 내용 다수 토큰 매칭됨."
        s["citations"] = []
        s["grounding"] = {"ok": True, "warnings": []}
        return s

    with patch("autonexusgraph.agents.graph.triage_node", triage), \
         patch("autonexusgraph.agents.graph.planner_node", planner), \
         patch("autonexusgraph.agents.graph.executor_node", executor), \
         patch("autonexusgraph.agents.graph.synthesizer_node", synth):
        state = run_agent("삼성전자 매출", thread_id="t-smoke-1")
    assert state["answer"].startswith("정상 답변")
    assert state.get("n_replans", 0) == 0
    assert state.get("validation_status") == "passed"


def test_run_agent_replan_then_pass():
    """1차 답변 fail → replan → 2차 답변 통과."""
    call_count = {"synth": 0}

    def triage(s):
        s["question_kind"] = "factual"
        s["target_companies"] = ["00126380"]
        return s

    def planner(s):
        s["plan"] = [{"tool": "x", "purpose": "y"}]
        return s

    def executor(s):
        s["tool_results"] = [{"tool": "x", "result": "OK"}]
        s["evidence_chunks"] = [{"text": "본문"}]
        return s

    def synth(s):
        call_count["synth"] += 1
        if call_count["synth"] == 1:
            # 1차 — 너무 짧아 fail
            s["answer"] = "짧음"
        else:
            s["answer"] = "두 번째 시도에서 충분히 긴 답변을 만들었습니다. [출처: 00126380]"
        s["citations"] = []
        s["grounding"] = {"ok": True, "warnings": []}
        return s

    with patch("autonexusgraph.agents.graph.triage_node", triage), \
         patch("autonexusgraph.agents.graph.planner_node", planner), \
         patch("autonexusgraph.agents.graph.executor_node", executor), \
         patch("autonexusgraph.agents.graph.synthesizer_node", synth):
        state = run_agent("삼성전자 매출", thread_id="t-smoke-2")
    assert state["n_replans"] >= 1
    assert state["validation_status"] == "passed"
    assert "두 번째" in state["answer"]


def test_run_agent_replan_exhausted_emits_warning():
    """모든 replan 실패 → validation_issues 명시 + ⚠️ prefix."""
    def triage(s):
        s["question_kind"] = "factual"
        s["target_companies"] = ["00126380"]
        return s

    def planner(s):
        s["plan"] = []
        return s

    def executor(s):
        s["tool_results"] = []
        s["evidence_chunks"] = []
        return s

    def synth(s):
        s["answer"] = "X"   # 너무 짧음 → 항상 fail
        s["grounding"] = {"ok": True, "warnings": []}
        return s

    with patch("autonexusgraph.agents.graph.triage_node", triage), \
         patch("autonexusgraph.agents.graph.planner_node", planner), \
         patch("autonexusgraph.agents.graph.executor_node", executor), \
         patch("autonexusgraph.agents.graph.synthesizer_node", synth):
        state = run_agent("X", thread_id="t-smoke-3")
    assert state["n_replans"] == 2   # MAX_REPLANS
    assert state["validation_status"] == "failed"
    assert "⚠️" in state["answer"]
    assert "answer_too_short" in state["answer"]
