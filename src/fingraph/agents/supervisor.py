"""Supervisor 노드 — DAG 의존성 충족된 task 를 worker 로 라우팅 (PRD §7.5.2 / §7.5.7).

두 가지 실행 모드:
- 함수 체인 (langgraph 미설치) — supervisor 가 unblocked tasks 를 순차 dispatch.
  의존성 없는 task 도 sequential (LangGraph 가 있어야 진정한 병렬).
- LangGraph (StateGraph) — supervisor 가 ``Send`` 객체 리스트를 yield 하면
  langgraph 가 worker 노드들을 병렬 실행하고 다시 supervisor 로 합류.

turn budget / circuit breaker 체크는 worker 진입 직전에도 다시 한다 (worker 가
호출하는 도구 안에서 LLM 발생 가능).
"""

from __future__ import annotations

import logging
from typing import Any

from .dag import all_done, task_summary, topologically_valid, unblocked_tasks
from .policy import turn_budget_exceeded
from .state import AgentState
from .workers import dispatch_one

log = logging.getLogger(__name__)


def supervisor_node(state: AgentState) -> AgentState:
    """함수 체인용 — unblocked tasks 를 모두 sequential dispatch.

    같은 turn 내에서 반복 호출되며 (StateGraph 의 self-loop 와 동일 효과), 더
    이상 unblocked 가 없으면 noop. ``all_done`` 검사로 종결 시점 결정.
    """
    tasks: list[dict] = state.get("tasks") or []
    if not tasks:
        return state

    if not topologically_valid(tasks):
        log.warning("[supervisor] task DAG 순환·미정의 의존성 — 모두 skipped")
        for t in tasks:
            if t.get("status") == "pending":
                t["status"] = "skipped"
                t["result"] = {"error": "invalid_dag"}
        return state

    while True:
        if turn_budget_exceeded(state):
            log.warning("[supervisor] turn budget exceeded — 잔여 task skip")
            for t in tasks:
                if t.get("status") == "pending":
                    t["status"] = "skipped"
                    t["result"] = {"error": "turn_budget"}
            state["aborted_reason"] = "turn_budget"
            break

        ready = unblocked_tasks(tasks)
        if not ready:
            break
        # sequential — 의존성 없는 ready 도 한 번에 하나씩.
        # LangGraph Send 경로에서는 sup_send_directives() 가 병렬 dispatch 한다.
        for t in ready:
            if t.get("status") != "pending":
                continue
            t["status"] = "running"
            dispatch_one(state, t)
        # done/failed/skipped 로 옮겨졌으므로 다음 라운드의 unblocked 가 변동
    log.info("[supervisor] tasks done — summary=%s", task_summary(tasks))
    return state


def sup_send_directives(state: AgentState):
    """LangGraph Send API 용 — unblocked tasks 만큼 Send 객체 리스트.

    각 Send 는 worker 노드를 가리키며 args 로 자기 task 를 전달한다. 반환값이
    빈 리스트면 langgraph 가 conditional edge 의 'done' 경로로 이동한다.
    """
    try:
        from langgraph.types import Send   # type: ignore[import-not-found]
    except ImportError:
        try:
            from langgraph.graph import Send   # type: ignore[attr-defined]
        except ImportError:
            return []

    tasks: list[dict] = state.get("tasks") or []
    if not tasks or not topologically_valid(tasks):
        return []
    if turn_budget_exceeded(state):
        return []

    ready = unblocked_tasks(tasks)
    if not ready:
        return []

    # worker 노드명은 graph.py 의 add_node 명과 일치해야 한다.
    NODE_BY_AGENT = {
        "research": "worker_research",
        "graph": "worker_graph",
        "sql": "worker_sql",
        "calculator": "worker_calculator",
    }
    sends = []
    for t in ready:
        node = NODE_BY_AGENT.get(str(t.get("agent")))
        if not node:
            t["status"] = "skipped"
            t["result"] = {"error": f"unknown agent: {t.get('agent')!r}"}
            continue
        t["status"] = "running"
        # 각 Send 는 child invocation 의 입력 state — task 와 전체 state 모두 전달
        sends.append(Send(node, {**state, "_current_task": t}))
    return sends


def supervisor_done(state: AgentState) -> str:
    """라우터 — 모든 task 완료면 'synth' 로, 아니면 'dispatch' 반복."""
    tasks = state.get("tasks") or []
    if not tasks or all_done(tasks):
        return "done"
    return "dispatch"


__all__ = ["supervisor_node", "sup_send_directives", "supervisor_done"]
