"""Worker 노드 4종 — Research / Graph / SQL / Calculator (PRD §7.5.2).

각 worker:
- AgentState + 자기 task 1개 받음
- 자기 도메인 도구만 호출 (도구 외 접근 금지 — 라우팅 단계에서 검증)
- result 채워서 task 갱신
- worker 실패는 state["aborted_reason"] 안 채움 — task.status="failed" 만 표시
  (Supervisor 가 다른 task 로 계속 진행, Validator 가 최종 판단)

PRD §7.5.11 — Calculator 의 Python sandbox 는 e2b/daytona 인프라 도입 시 교체.
이번 PR 은 ``_safe_calculator()`` 의 numexpr 기반 한정 evaluator — exec/eval/import/
attribute access 모두 금지. 사칙연산·비교·numpy 함수만 허용.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .dag import update_status
from .state import AgentState

log = logging.getLogger(__name__)


# ── Research worker ─────────────────────────────────────────
def research_worker(state: AgentState, task: dict) -> AgentState:
    """벡터 검색 (pgvector + 메타 필터)."""
    from ..tools.retrieve import search_documents, search_by_metadata, get_chunk

    intent = task.get("intent") or "search"
    args = dict(task.get("args") or {})

    try:
        if intent == "search_documents":
            out = search_documents(**args)
        elif intent == "search_by_metadata":
            out = search_by_metadata(**args)
        elif intent == "get_chunk":
            out = get_chunk(**args)
        else:
            # 기본은 search_documents — args 에 query 가 있어야 함
            args.setdefault("query", state.get("question_rewritten") or state.get("question", ""))
            out = search_documents(**args)
        _record(state, task, status="done", result=out)
        if isinstance(out, list):
            state.setdefault("evidence_chunks", []).extend(out)
    except Exception as exc:   # noqa: BLE001
        log.warning("[research] %s failed: %s", intent, exc)
        _record(state, task, status="failed", result={"error": str(exc)})
    return state


# ── Graph worker ────────────────────────────────────────────
def graph_worker(state: AgentState, task: dict) -> AgentState:
    """Neo4j 관계 탐색 (cypher_guard 통과). args 의 intent 가 함수명."""
    from .. import tools as toolbox

    intent = task.get("intent") or ""
    args = dict(task.get("args") or {})

    allowed = {
        "list_subsidiaries", "list_parents", "get_executives",
        "get_companies_of_person", "get_major_shareholders",
        "find_paths", "get_subgraph", "list_mentioning_news",
        "list_cooccurring", "list_group_members", "lookup_person",
    }
    if intent not in allowed:
        _record(state, task, status="skipped",
                result={"error": f"graph intent 미허용: {intent!r}"})
        return state
    fn = getattr(toolbox, intent, None)
    if fn is None:
        _record(state, task, status="failed", result={"error": f"no such tool: {intent}"})
        return state
    try:
        out = fn(**args)
        _record(state, task, status="done", result=out)
        if intent == "get_subgraph":
            state["graph_subgraph"] = out
    except Exception as exc:   # noqa: BLE001
        log.warning("[graph] %s failed: %s", intent, exc)
        _record(state, task, status="failed", result={"error": str(exc)})
    return state


# ── SQL worker ──────────────────────────────────────────────
def sql_worker(state: AgentState, task: dict) -> AgentState:
    """PG 정형 조회. 사전 정의 함수 풀만 (PRD §7.5.10)."""
    from .. import tools as toolbox

    intent = task.get("intent") or ""
    args = dict(task.get("args") or {})

    allowed = {
        "lookup_company", "get_company_info", "get_revenue",
        "get_operating_income", "get_balance_sheet_item",
        "compare_companies", "list_companies_by_market",
    }
    if intent not in allowed:
        _record(state, task, status="skipped",
                result={"error": f"sql intent 미허용: {intent!r}"})
        return state
    fn = getattr(toolbox, intent, None)
    if fn is None:
        _record(state, task, status="failed", result={"error": f"no such tool: {intent}"})
        return state
    try:
        out = fn(**args)
        _record(state, task, status="done", result=out)
    except Exception as exc:   # noqa: BLE001
        log.warning("[sql] %s failed: %s", intent, exc)
        _record(state, task, status="failed", result={"error": str(exc)})
    return state


# ── Calculator worker ───────────────────────────────────────
# 안전 evaluator — exec/eval/import/attribute access 금지.
# Python sandbox (e2b/daytona) 도입 전 1차 구현. 사칙연산·비교·numpy 함수만.
_EXPR_ALLOWED_RE = re.compile(
    r"^[\d\s\.,\+\-\*\/\%\(\)\<\>\=\!\&\|\^a-zA-Z_]+$"
)


def calculator_worker(state: AgentState, task: dict) -> AgentState:
    """수식 평가. task.args:
       - expr: str — 평가할 수식 (필수)
       - variables: dict[str, number] — expr 안 변수 바인딩 (선택)
       - aggregate: 'sum'|'mean'|'max'|'min'|'count' + over: list — 집계 (선택)
    """
    args = dict(task.get("args") or {})

    try:
        if "aggregate" in args and "over" in args:
            result = _aggregate(args["aggregate"], args["over"])
        else:
            result = _safe_calculator(
                args.get("expr") or "",
                args.get("variables") or {},
            )
        _record(state, task, status="done", result={"value": result})
    except Exception as exc:   # noqa: BLE001
        log.warning("[calculator] failed: %s", exc)
        _record(state, task, status="failed", result={"error": str(exc)})
    return state


def _safe_calculator(expr: str, variables: dict) -> float:
    """numexpr 기반 안전 평가. numexpr 미설치 시 정적 검사 후 eval (제한된 namespace)."""
    if not expr or not isinstance(expr, str):
        raise ValueError("expr 필요")
    # 1차 정적 가드 — 허용 문자만
    if not _EXPR_ALLOWED_RE.match(expr):
        raise ValueError(f"허용되지 않은 문자 포함: {expr!r}")
    # 위험 키워드 차단
    BAD = ("import", "exec", "eval", "open", "__", "lambda", "compile",
           "globals", "locals", "vars", "getattr", "setattr", "delattr",
           "type", "object", "subprocess", "os.")
    for w in BAD:
        if w in expr:
            raise ValueError(f"금지 키워드: {w}")
    # 변수 타입 검증 — number 만
    safe_vars: dict[str, float] = {}
    for k, v in (variables or {}).items():
        if not isinstance(k, str) or not k.isidentifier():
            raise ValueError(f"식별자 아닌 변수: {k!r}")
        if not isinstance(v, (int, float)):
            raise ValueError(f"숫자 아닌 변수값: {k}={v!r}")
        safe_vars[k] = float(v)

    try:
        import numexpr   # type: ignore[import-not-found]
        return float(numexpr.evaluate(expr, local_dict=safe_vars, global_dict={}).item())
    except ImportError:
        # 폴백 — 매우 제한된 builtins (산술만)
        return float(eval(expr,   # noqa: S307 — guarded above
                           {"__builtins__": {}}, safe_vars))


def _aggregate(op: str, values: list) -> float:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if not nums:
        return 0.0
    if op == "sum":
        return sum(nums)
    if op == "mean":
        return sum(nums) / len(nums)
    if op == "max":
        return max(nums)
    if op == "min":
        return min(nums)
    if op == "count":
        return float(len(nums))
    raise ValueError(f"미지원 집계: {op}")


# ── 공통 기록 헬퍼 ──────────────────────────────────────────
def _record(state: AgentState, task: dict, *,
            status: str, result: Any) -> None:
    """task.status / task.result 갱신 + state.task_results / tool_results 누적."""
    tasks = state.get("tasks") or []
    update_status(tasks, task["id"], status, result=result)
    task_results = state.setdefault("task_results", {})
    task_results[task["id"]] = result
    # 기존 호환 — synthesizer 가 tool_results 를 참조하므로 그대로 채움
    state.setdefault("tool_results", []).append({
        "tool": task.get("intent"),
        "purpose": task.get("intent"),
        "args": task.get("args"),
        "result": result,
        "agent": task.get("agent"),
        "task_id": task.get("id"),
        "status": status,
    })


# ── Worker 디스패치 테이블 ──────────────────────────────────
WORKER_BY_AGENT = {
    "research": research_worker,
    "graph": graph_worker,
    "sql": sql_worker,
    "calculator": calculator_worker,
}


def dispatch_one(state: AgentState, task: dict) -> AgentState:
    """단일 task 의 agent 에 맞는 worker 호출. agent 미지정 시 skipped."""
    agent = task.get("agent")
    worker = WORKER_BY_AGENT.get(str(agent))
    if worker is None:
        _record(state, task, status="skipped",
                result={"error": f"unknown agent: {agent!r}"})
        return state
    return worker(state, task)


__all__ = [
    "research_worker",
    "graph_worker",
    "sql_worker",
    "calculator_worker",
    "dispatch_one",
    "WORKER_BY_AGENT",
]
