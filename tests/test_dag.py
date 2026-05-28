"""DAG 헬퍼 단위 테스트."""

from __future__ import annotations

from autonexusgraph.agents.dag import (
    all_done,
    filter_by_agent,
    get_task,
    make_task,
    task_summary,
    topologically_valid,
    unblocked_tasks,
    update_status,
)


def test_make_task_defaults():
    t = make_task("t1", "sql", "get_revenue", {"corp_code": "00126380"})
    assert t == {
        "id": "t1", "agent": "sql", "intent": "get_revenue",
        "args": {"corp_code": "00126380"}, "depends_on": [],
        "status": "pending", "result": None,
    }


def test_make_task_with_deps():
    t = make_task("t2", "sql", "get_op", depends_on=["t1"])
    assert t["depends_on"] == ["t1"]


def test_unblocked_no_deps_all_pending():
    tasks = [
        make_task("a", "sql", "x"),
        make_task("b", "graph", "y"),
    ]
    ready = unblocked_tasks(tasks)
    assert {t["id"] for t in ready} == {"a", "b"}


def test_unblocked_respects_deps():
    tasks = [
        make_task("a", "graph", "list_subs"),
        make_task("b", "sql", "get_rev", depends_on=["a"]),
    ]
    # 처음엔 a 만 ready
    ready1 = unblocked_tasks(tasks)
    assert {t["id"] for t in ready1} == {"a"}
    # a 완료 후 b ready
    update_status(tasks, "a", "done", result=[])
    ready2 = unblocked_tasks(tasks)
    assert {t["id"] for t in ready2} == {"b"}


def test_unblocked_skips_running_and_done():
    tasks = [make_task("a", "sql", "x"), make_task("b", "sql", "y")]
    update_status(tasks, "a", "running")
    update_status(tasks, "b", "done")
    assert unblocked_tasks(tasks) == []


def test_all_done_terminal_states():
    tasks = [
        make_task("a", "sql", "x"),
        make_task("b", "sql", "y"),
        make_task("c", "sql", "z"),
    ]
    assert not all_done(tasks)
    update_status(tasks, "a", "done")
    update_status(tasks, "b", "failed")
    update_status(tasks, "c", "skipped")
    assert all_done(tasks)


def test_all_done_empty():
    assert all_done([])


def test_get_task_found_and_missing():
    tasks = [make_task("a", "sql", "x")]
    assert get_task(tasks, "a")["id"] == "a"
    assert get_task(tasks, "nope") is None


def test_update_status_with_result():
    tasks = [make_task("a", "sql", "x")]
    update_status(tasks, "a", "done", result={"value": 42})
    assert tasks[0]["status"] == "done"
    assert tasks[0]["result"] == {"value": 42}


def test_task_summary():
    tasks = [
        make_task("a", "sql", "x"),
        make_task("b", "sql", "y"),
        make_task("c", "sql", "z"),
    ]
    update_status(tasks, "a", "done")
    update_status(tasks, "b", "running")
    s = task_summary(tasks)
    assert s["total"] == 3
    assert s.get("done") == 1
    assert s.get("running") == 1
    assert s.get("pending") == 1


def test_topologically_valid_normal_dag():
    tasks = [
        make_task("a", "g", "x"),
        make_task("b", "s", "y", depends_on=["a"]),
        make_task("c", "s", "z", depends_on=["a", "b"]),
    ]
    assert topologically_valid(tasks)


def test_topologically_invalid_cycle():
    tasks = [
        make_task("a", "g", "x", depends_on=["b"]),
        make_task("b", "s", "y", depends_on=["a"]),
    ]
    assert not topologically_valid(tasks)


def test_topologically_invalid_unknown_dep():
    tasks = [
        make_task("a", "g", "x", depends_on=["missing"]),
    ]
    assert not topologically_valid(tasks)


def test_filter_by_agent():
    tasks = [
        make_task("a", "sql", "x"),
        make_task("b", "graph", "y"),
        make_task("c", "sql", "z"),
    ]
    assert {t["id"] for t in filter_by_agent(tasks, "sql")} == {"a", "c"}
    assert filter_by_agent(tasks, "calculator") == []
