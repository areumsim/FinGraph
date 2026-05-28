"""task DAG 보조 함수 — Supervisor / Send API 용.

PRD §7.5.3 의 tasks 스키마:
    {"id": str, "agent": AgentName, "intent": str, "args": dict,
     "depends_on": list[str], "status": TaskStatus, "result": Any}

DAG 자체는 list 로 직렬화돼 AgentState["tasks"] 에 들어간다 (LangGraph
state 가 dict 만 받기 때문에 graph object 는 안 만든다). 의존성·실행 순서는
이 모듈의 함수들이 결정한다.
"""

from __future__ import annotations

from typing import Iterable


def make_task(
    task_id: str,
    agent: str,
    intent: str,
    args: dict | None = None,
    depends_on: list[str] | None = None,
) -> dict:
    """tasks 항목 생성 — 기본 status='pending', result=None."""
    return {
        "id": task_id,
        "agent": agent,
        "intent": intent,
        "args": args or {},
        "depends_on": list(depends_on or []),
        "status": "pending",
        "result": None,
    }


def unblocked_tasks(tasks: list[dict]) -> list[dict]:
    """의존성 충족 + 아직 pending 인 task 들. Supervisor 가 다음 디스패치 대상."""
    done_ids = {t["id"] for t in tasks if t.get("status") == "done"}
    out: list[dict] = []
    for t in tasks:
        if t.get("status") != "pending":
            continue
        deps = t.get("depends_on") or []
        if all(d in done_ids for d in deps):
            out.append(t)
    return out


def all_done(tasks: list[dict]) -> bool:
    """모든 task 가 done / failed / skipped — Supervisor 가 다음 단계로 이동."""
    if not tasks:
        return True
    return all(t.get("status") in ("done", "failed", "skipped") for t in tasks)


def get_task(tasks: list[dict], task_id: str) -> dict | None:
    for t in tasks:
        if t.get("id") == task_id:
            return t
    return None


def update_status(tasks: list[dict], task_id: str, status: str,
                  result: object | None = None) -> list[dict]:
    """status / result 갱신. 원본 리스트를 in-place 수정 (state 도 같은 list 참조)."""
    for t in tasks:
        if t.get("id") == task_id:
            t["status"] = status
            if result is not None:
                t["result"] = result
            return tasks
    return tasks


def task_summary(tasks: list[dict]) -> dict:
    """디버그·로깅용 카운트."""
    out: dict[str, int] = {}
    for t in tasks:
        st = str(t.get("status") or "pending")
        out[st] = out.get(st, 0) + 1
    out["total"] = len(tasks)
    return out


def topologically_valid(tasks: list[dict]) -> bool:
    """순환 의존성이 없는지 — DAG 무결성 정적 검증."""
    ids = {t["id"] for t in tasks}
    # 알 수 없는 dep 참조 → invalid
    for t in tasks:
        for d in t.get("depends_on") or []:
            if d not in ids:
                return False
    # cycle 검출 — 간단한 DFS
    visited: set[str] = set()
    stack: set[str] = set()

    def _dfs(node: str, by_id: dict[str, dict]) -> bool:
        if node in stack:
            return False
        if node in visited:
            return True
        stack.add(node)
        for d in by_id[node].get("depends_on") or []:
            if not _dfs(d, by_id):
                return False
        stack.discard(node)
        visited.add(node)
        return True

    by_id = {t["id"]: t for t in tasks}
    return all(_dfs(t["id"], by_id) for t in tasks)


def filter_by_agent(tasks: Iterable[dict], agent: str) -> list[dict]:
    return [t for t in tasks if t.get("agent") == agent]


__all__ = [
    "make_task",
    "unblocked_tasks",
    "all_done",
    "get_task",
    "update_status",
    "task_summary",
    "topologically_valid",
    "filter_by_agent",
]
