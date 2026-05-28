"""run_agent_stream — 노드별 yield + 폴백 체인 모두 검증."""

from __future__ import annotations

from unittest.mock import patch

from autonexusgraph.agents.graph import run_agent_stream


def test_stream_yields_node_sequence_clean_path():
    """폴백 체인: triage → planner → executor → synthesizer → validator → __final__."""
    def triage(s):
        s["question_kind"] = "factual"
        s["target_companies"] = ["00126380"]
        return s

    def planner(s):
        s["plan"] = [{"tool": "x", "purpose": "p"}]
        return s

    def executor(s):
        s["tool_results"] = [{"tool": "x", "result": "ok"}]
        s["evidence_chunks"] = [{"text": "본문 내용 충분"}]
        return s

    def synth(s):
        s["answer"] = "정상 답변입니다. 본문 내용 충분히 매칭."
        s["grounding"] = {"ok": True, "warnings": []}
        return s

    with patch("autonexusgraph.agents.graph.triage_node", triage), \
         patch("autonexusgraph.agents.graph.planner_node", planner), \
         patch("autonexusgraph.agents.graph.executor_node", executor), \
         patch("autonexusgraph.agents.graph.synthesizer_node", synth):
        events = list(run_agent_stream("삼성전자 매출", thread_id="t-stream-1"))

    nodes = [n for n, _ in events]
    # langgraph 가 있으면 finalize 노드도 포함, 없으면 폴백 체인 형태
    assert "triage" in nodes
    assert "__final__" == nodes[-1]
    # 마지막 state 의 answer 가 채워졌는지
    final_state = events[-1][1]
    assert "정상" in final_state["answer"]


def test_stream_replan_emits_replan_event():
    """1차 fail → replan node yield → 2차 pass."""
    call = {"synth": 0}

    def triage(s):
        s["question_kind"] = "factual"
        s["target_companies"] = ["00126380"]
        return s

    def planner(s):
        s["plan"] = [{"tool": "x"}]
        return s

    def executor(s):
        s["tool_results"] = [{"tool": "x", "result": "ok"}]
        s["evidence_chunks"] = [{"text": "본문"}]
        return s

    def synth(s):
        call["synth"] += 1
        if call["synth"] == 1:
            s["answer"] = "짧"   # fail
        else:
            s["answer"] = "두 번째 시도에서 충분히 긴 답변을 만들었습니다."
        s["grounding"] = {"ok": True, "warnings": []}
        return s

    with patch("autonexusgraph.agents.graph.triage_node", triage), \
         patch("autonexusgraph.agents.graph.planner_node", planner), \
         patch("autonexusgraph.agents.graph.executor_node", executor), \
         patch("autonexusgraph.agents.graph.synthesizer_node", synth):
        events = list(run_agent_stream("X", thread_id="t-stream-2"))

    nodes = [n for n, _ in events]
    # langgraph 가 있으면 planner 가 2번, 폴백이면 replan 노드도 명시
    n_planner = nodes.count("planner")
    assert n_planner >= 2 or "replan" in nodes
    final = events[-1][1]
    assert "두 번째" in final["answer"]


def test_stream_partial_state_progresses():
    """각 yield 시점에 partial state 가 누적되어야 함."""
    def triage(s):
        s["question_kind"] = "structural"
        s["target_companies"] = ["00126380", "00164779"]
        return s

    def planner(s):
        s["plan"] = [{"tool": "list_subsidiaries"}, {"tool": "get_executives"}]
        return s

    def executor(s):
        s["tool_results"] = [
            {"tool": "list_subsidiaries", "result": [{"child_name": "삼성디스플레이"}]},
            {"tool": "get_executives", "result": [{"name": "한종희"}]},
        ]
        s["evidence_chunks"] = []
        return s

    def synth(s):
        s["answer"] = "자회사와 임원진 정보를 조회한 결과입니다."
        s["grounding"] = {"ok": True, "warnings": []}
        return s

    with patch("autonexusgraph.agents.graph.triage_node", triage), \
         patch("autonexusgraph.agents.graph.planner_node", planner), \
         patch("autonexusgraph.agents.graph.executor_node", executor), \
         patch("autonexusgraph.agents.graph.synthesizer_node", synth):
        events = list(run_agent_stream("자회사·임원진", thread_id="t-stream-3"))

    # triage 직후 시점의 target_companies 가 있어야 함
    triage_events = [(n, s) for n, s in events if n == "triage"]
    assert triage_events
    assert triage_events[0][1].get("target_companies") == ["00126380", "00164779"]
    # executor 후 tool_results 2건
    executor_events = [(n, s) for n, s in events if n == "executor"]
    assert executor_events
    assert len(executor_events[0][1].get("tool_results") or []) == 2
