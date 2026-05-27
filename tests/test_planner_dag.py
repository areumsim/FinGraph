"""Planner DAG 산출 검증 — question_kind 별 task 구성."""

from __future__ import annotations

from fingraph.agents.nodes import planner_node


def _state(kind, targets=None, q="질문"):
    return {
        "question": q,
        "question_rewritten": q,
        "question_kind": kind,
        "target_companies": targets or [],
        "llm_usage_usd": 0.0,
    }


def test_factual_produces_sql_tasks_per_company():
    s = _state("factual", targets=["00126380", "00164779"], q="삼성전자 2024년 매출")
    planner_node(s)
    tasks = s["tasks"]
    sql_tasks = [t for t in tasks if t["agent"] == "sql"]
    assert len(sql_tasks) == 4   # 2 회사 × (get_revenue + get_operating_income)
    intents = {t["intent"] for t in sql_tasks}
    assert intents == {"get_revenue", "get_operating_income"}


def test_structural_produces_graph_tasks_parallel():
    s = _state("structural", targets=["00126380"])
    planner_node(s)
    tasks = s["tasks"]
    # 3 intent × 1 회사 = 3 task, 의존성 없음
    assert len(tasks) == 3
    assert all(t["agent"] == "graph" for t in tasks)
    assert all(not t["depends_on"] for t in tasks)
    intents = {t["intent"] for t in tasks}
    assert intents == {"list_subsidiaries", "get_executives", "get_major_shareholders"}


def test_narrative_produces_one_research_task():
    s = _state("narrative", targets=["00126380"], q="삼성전자 사업 위험요인")
    planner_node(s)
    tasks = s["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["agent"] == "research"
    assert tasks[0]["intent"] == "search_documents"


def test_multi_hop_chains_sql_after_graph():
    s = _state("multi_hop", targets=["00126380"], q="삼성전자 자회사 매출")
    planner_node(s)
    tasks = s["tasks"]
    graph_tasks = [t for t in tasks if t["agent"] == "graph"]
    sql_tasks = [t for t in tasks if t["agent"] == "sql"]
    research_tasks = [t for t in tasks if t["agent"] == "research"]
    assert graph_tasks and sql_tasks and research_tasks
    # SQL 은 모든 graph task 에 의존
    graph_ids = {t["id"] for t in graph_tasks}
    for st in sql_tasks:
        assert set(st["depends_on"]) == graph_ids
    # research 는 의존성 없음
    assert all(not t["depends_on"] for t in research_tasks)


def test_unknown_falls_back_to_research():
    s = _state("unknown", q="모호한 질문")
    planner_node(s)
    tasks = s["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["agent"] == "research"


def test_legacy_plan_also_populated_for_executor_fallback():
    """tasks 가 비어도 외부 호출자는 plan 으로 작동할 수 있어야 한다."""
    s = _state("factual", targets=["00126380"])
    planner_node(s)
    # plan 은 항상 tasks 와 1:1 미러
    assert len(s["plan"]) == len(s["tasks"])
    for t, p in zip(s["tasks"], s["plan"]):
        assert p["tool"] == t["intent"]
        assert p["args"] == t["args"]


def test_no_targets_factual_produces_empty():
    """factual + 회사 없음 → SQL task 0 (search 만 unknown 으로 fall through 안 함)."""
    s = _state("factual", targets=[])
    planner_node(s)
    assert s["tasks"] == []


def test_factual_uses_year_hint():
    """질문에서 연도 추출 → SQL args.year 에 반영."""
    s = _state("factual", targets=["00126380"], q="삼성전자 2022년 매출")
    planner_node(s)
    rev_tasks = [t for t in s["tasks"] if t["intent"] == "get_revenue"]
    assert rev_tasks
    assert rev_tasks[0]["args"]["year"] == 2022
