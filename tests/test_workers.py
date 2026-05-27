"""Worker 4종 단위 테스트 — 도구를 mock 으로 격리."""

from __future__ import annotations

from unittest.mock import patch

from fingraph.agents.dag import make_task
from fingraph.agents.workers import (
    calculator_worker,
    dispatch_one,
    graph_worker,
    research_worker,
    sql_worker,
)


def _make_state(tasks):
    return {"question": "q", "question_rewritten": "q", "tasks": tasks,
            "task_results": {}, "llm_usage_usd": 0.0}


# ── Research ────────────────────────────────────────────────
def test_research_search_documents():
    task = make_task("r1", "research", "search_documents", {"query": "삼성", "top_k": 3})
    state = _make_state([task])
    with patch("fingraph.tools.retrieve.search_documents",
                lambda **kw: [{"id": "c1", "text": "본문"}]):
        research_worker(state, task)
    assert task["status"] == "done"
    assert state["task_results"]["r1"][0]["id"] == "c1"
    assert state["evidence_chunks"]   # search 결과는 evidence 에도 누적


def test_research_unknown_intent_falls_back_to_search():
    task = make_task("r2", "research", "weird_intent", {"query": "x"})
    state = _make_state([task])
    with patch("fingraph.tools.retrieve.search_documents",
                lambda **kw: [{"id": "c1"}]):
        research_worker(state, task)
    assert task["status"] == "done"


def test_research_failure_marks_task_failed():
    task = make_task("r3", "research", "search_documents", {"query": "x"})
    state = _make_state([task])
    def _boom(**kw):
        raise RuntimeError("db down")
    with patch("fingraph.tools.retrieve.search_documents", _boom):
        research_worker(state, task)
    assert task["status"] == "failed"
    assert "error" in task["result"]


# ── Graph ───────────────────────────────────────────────────
def test_graph_allowed_intent():
    task = make_task("g1", "graph", "list_subsidiaries", {"parent_corp_code": "00126380"})
    state = _make_state([task])
    import fingraph.tools as toolbox
    with patch.object(toolbox, "list_subsidiaries",
                       lambda **kw: [{"child_name": "삼성디스플레이"}], create=True):
        graph_worker(state, task)
    assert task["status"] == "done"
    assert task["result"][0]["child_name"] == "삼성디스플레이"


def test_graph_disallowed_intent_skipped():
    task = make_task("g2", "graph", "DROP_DATABASE", {})
    state = _make_state([task])
    graph_worker(state, task)
    assert task["status"] == "skipped"


def test_graph_subgraph_populates_state():
    task = make_task("g3", "graph", "get_subgraph", {"corp_code": "x", "depth": 1})
    state = _make_state([task])
    import fingraph.tools as toolbox
    with patch.object(toolbox, "get_subgraph",
                       lambda **kw: {"nodes": [], "edges": []}, create=True):
        graph_worker(state, task)
    assert state.get("graph_subgraph") == {"nodes": [], "edges": []}


# ── SQL ─────────────────────────────────────────────────────
def test_sql_allowed_intent():
    task = make_task("s1", "sql", "get_revenue", {"corp_code": "00126380", "year": 2023})
    state = _make_state([task])
    import fingraph.tools as toolbox
    with patch.object(toolbox, "get_revenue",
                       lambda **kw: {"value": 258_935_500_000_000}, create=True):
        sql_worker(state, task)
    assert task["status"] == "done"
    assert task["result"]["value"] == 258_935_500_000_000


def test_sql_disallowed_intent_skipped():
    task = make_task("s2", "sql", "DELETE_TABLE", {})
    state = _make_state([task])
    sql_worker(state, task)
    assert task["status"] == "skipped"


# ── Calculator ──────────────────────────────────────────────
def test_calculator_basic_expr():
    task = make_task("c1", "calculator", "eval", {"expr": "1 + 2 * 3"})
    state = _make_state([task])
    calculator_worker(state, task)
    assert task["status"] == "done"
    assert task["result"]["value"] == 7.0


def test_calculator_with_variables():
    task = make_task("c2", "calculator", "eval",
                     {"expr": "a * b", "variables": {"a": 5, "b": 6}})
    state = _make_state([task])
    calculator_worker(state, task)
    assert task["result"]["value"] == 30.0


def test_calculator_aggregate_sum():
    task = make_task("c3", "calculator", "agg",
                     {"aggregate": "sum", "over": [1, 2, 3, 4]})
    state = _make_state([task])
    calculator_worker(state, task)
    assert task["result"]["value"] == 10.0


def test_calculator_aggregate_mean():
    task = make_task("c4", "calculator", "agg",
                     {"aggregate": "mean", "over": [10, 20, 30]})
    state = _make_state([task])
    calculator_worker(state, task)
    assert task["result"]["value"] == 20.0


def test_calculator_blocks_injection_keyword():
    task = make_task("c5", "calculator", "eval", {"expr": "__import__('os')"})
    state = _make_state([task])
    calculator_worker(state, task)
    assert task["status"] == "failed"
    assert "금지" in task["result"]["error"] or "허용" in task["result"]["error"]


def test_calculator_blocks_disallowed_chars():
    task = make_task("c6", "calculator", "eval", {"expr": "1 + 2; print('hi')"})
    state = _make_state([task])
    calculator_worker(state, task)
    assert task["status"] == "failed"


def test_calculator_rejects_non_numeric_variables():
    task = make_task("c7", "calculator", "eval",
                     {"expr": "a + 1", "variables": {"a": "notanumber"}})
    state = _make_state([task])
    calculator_worker(state, task)
    assert task["status"] == "failed"


# ── dispatch_one ────────────────────────────────────────────
def test_dispatch_one_routes_to_correct_worker():
    task = make_task("d1", "calculator", "eval", {"expr": "2 + 2"})
    state = _make_state([task])
    dispatch_one(state, task)
    assert task["status"] == "done"
    assert task["result"]["value"] == 4.0


def test_dispatch_one_unknown_agent_skipped():
    task = make_task("d2", "ghost", "x", {})
    state = _make_state([task])
    dispatch_one(state, task)
    assert task["status"] == "skipped"
    assert "unknown agent" in task["result"]["error"]
