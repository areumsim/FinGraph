"""Supervisor — DAG 의존성 라우팅 + 함수 모드 sequential dispatch + Send 디렉티브."""

from __future__ import annotations

from unittest.mock import patch

from autonexusgraph.agents.dag import make_task
from autonexusgraph.agents.supervisor import (
    sup_send_directives,
    supervisor_done,
    supervisor_node,
)


def _state(tasks, **extra):
    s = {"tasks": tasks, "task_results": {}, "llm_usage_usd": 0.0,
         "question": "q", "question_rewritten": "q"}
    s.update(extra)
    return s


def test_supervisor_runs_all_when_no_deps():
    """4개 calculator task — 의존성 없음 → 함수 모드 전부 실행."""
    tasks = [
        make_task("c1", "calculator", "eval", {"expr": "1"}),
        make_task("c2", "calculator", "eval", {"expr": "2"}),
        make_task("c3", "calculator", "eval", {"expr": "3"}),
    ]
    s = _state(tasks)
    supervisor_node(s)
    assert all(t["status"] == "done" for t in tasks)
    assert [t["result"]["value"] for t in tasks] == [1.0, 2.0, 3.0]


def test_supervisor_respects_deps():
    """b 가 a 에 의존 — a 먼저 done, 그 후 b."""
    tasks = [
        make_task("a", "calculator", "eval", {"expr": "10"}),
        make_task("b", "calculator", "eval", {"expr": "20"}, depends_on=["a"]),
    ]
    s = _state(tasks)
    supervisor_node(s)
    assert tasks[0]["status"] == "done"
    assert tasks[1]["status"] == "done"


def test_supervisor_invalid_dag_marks_skipped():
    """순환 의존성 → 모두 skipped."""
    tasks = [
        make_task("a", "calculator", "eval", {"expr": "1"}, depends_on=["b"]),
        make_task("b", "calculator", "eval", {"expr": "2"}, depends_on=["a"]),
    ]
    s = _state(tasks)
    supervisor_node(s)
    assert all(t["status"] == "skipped" for t in tasks)


def test_supervisor_budget_exceeded_skips_remaining(monkeypatch):
    """turn budget 초과 시 잔여 task skip + aborted_reason."""
    tasks = [
        make_task("a", "calculator", "eval", {"expr": "1"}),
        make_task("b", "calculator", "eval", {"expr": "2"}),
    ]
    s = _state(tasks)
    with patch("autonexusgraph.agents.supervisor.turn_budget_exceeded", lambda st: True):
        supervisor_node(s)
    assert s.get("aborted_reason") == "turn_budget"
    assert all(t["status"] == "skipped" for t in tasks)


def test_supervisor_empty_tasks_noop():
    s = _state([])
    supervisor_node(s)
    assert s.get("aborted_reason") is None


def test_supervisor_done_routes_correctly():
    tasks_pending = [make_task("a", "calculator", "eval", {"expr": "1"})]
    s = _state(tasks_pending)
    assert supervisor_done(s) == "dispatch"

    tasks_pending[0]["status"] = "done"
    assert supervisor_done(s) == "done"

    assert supervisor_done(_state([])) == "done"


def test_send_directives_empty_when_no_unblocked():
    """모두 done → Send 0개."""
    tasks = [make_task("a", "calculator", "eval", {"expr": "1"})]
    tasks[0]["status"] = "done"
    s = _state(tasks)
    sends = sup_send_directives(s)
    assert sends == []


def test_send_directives_returns_one_per_unblocked():
    """langgraph 설치돼 있는 환경에서 ready task 만큼 Send 객체."""
    try:
        from langgraph.types import Send   # noqa: F401
    except ImportError:
        try:
            from langgraph.graph import Send   # type: ignore[attr-defined]  # noqa: F401
        except ImportError:
            import pytest
            pytest.skip("langgraph 미설치 — Send API 검증 skip")

    tasks = [
        make_task("a", "calculator", "eval", {"expr": "1"}),
        make_task("b", "calculator", "eval", {"expr": "2"}),
    ]
    s = _state(tasks)
    sends = sup_send_directives(s)
    assert len(sends) == 2
    # Send 후 task 들이 running 으로 마킹
    assert all(t["status"] == "running" for t in tasks)


def test_send_directives_invalid_dag_returns_empty():
    tasks = [
        make_task("a", "calculator", "eval", {}, depends_on=["b"]),
        make_task("b", "calculator", "eval", {}, depends_on=["a"]),
    ]
    s = _state(tasks)
    assert sup_send_directives(s) == []
