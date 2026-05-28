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


# ── Domain-aware allowed intent + toolbox ────────────────────
# finance 도메인 화이트리스트 (기존 그대로).
_FIN_GRAPH_ALLOWED = {
    "list_subsidiaries", "list_parents", "get_executives",
    "get_companies_of_person", "get_major_shareholders",
    "find_paths", "get_subgraph", "list_mentioning_news",
    "list_cooccurring", "list_group_members", "lookup_person",
}
_FIN_SQL_ALLOWED = {
    "lookup_company", "get_company_info", "get_revenue",
    "get_operating_income", "get_balance_sheet_item",
    "compare_companies", "list_companies_by_market",
}
_FIN_RESEARCH_INTENTS = {"search_documents", "search_by_metadata", "get_chunk"}

# auto 도메인 화이트리스트 (autograph.tools 함수명).
_AUTO_GRAPH_ALLOWED = {
    "lookup_vehicle_graph", "lookup_supplier",
    "list_components", "list_systems_of_model", "list_models_with_system",
    "list_recalls_affecting",
    "list_investigations_affecting", "get_investigation_recall_chain",
    "get_suppliers_of_component", "get_vehicles_using_component",
    "find_vehicle_component_paths",
}
_AUTO_SQL_ALLOWED = {
    "lookup_vehicle", "get_vehicle_info", "get_spec",
    "compare_vehicles", "get_safety_rating",
    # bridge 도 SQL 워커가 호출 (PG 단일 호출)
    "bridge_corp_to_entity", "bridge_entity_to_corp",
    "bridge_sec_cik_to_entity", "bridge_entity_to_sec_cik",
    "get_oem_financials_sec",
    "cross_query",
}
_AUTO_RESEARCH_INTENTS = {
    "search_documents_auto", "search_by_metadata_auto", "get_chunk_auto",
}


def _domain(state: AgentState) -> str:
    return str(state.get("domain") or "finance").lower()


def _toolbox_for(state: AgentState):
    """도메인별 tool 함수 풀. cross_domain 은 finance + auto 모두 검색."""
    d = _domain(state)
    if d == "auto":
        from autograph import tools as auto_tb
        return [auto_tb]
    if d == "cross_domain":
        from .. import tools as fin_tb
        from autograph import tools as auto_tb
        return [auto_tb, fin_tb]
    from .. import tools as fin_tb
    return [fin_tb]


def _resolve_tool(state: AgentState, intent: str):
    """intent 이름으로 도메인별 toolbox 에서 함수 검색."""
    for tb in _toolbox_for(state):
        fn = getattr(tb, intent, None)
        if fn is not None:
            return fn
    return None


def _allowed_intents(state: AgentState, kind: str) -> set[str]:
    d = _domain(state)
    if kind == "graph":
        if d == "auto":
            return _AUTO_GRAPH_ALLOWED
        if d == "cross_domain":
            return _FIN_GRAPH_ALLOWED | _AUTO_GRAPH_ALLOWED
        return _FIN_GRAPH_ALLOWED
    if kind == "sql":
        if d == "auto":
            return _AUTO_SQL_ALLOWED
        if d == "cross_domain":
            return _FIN_SQL_ALLOWED | _AUTO_SQL_ALLOWED
        return _FIN_SQL_ALLOWED
    if kind == "research":
        if d == "auto":
            return _AUTO_RESEARCH_INTENTS
        if d == "cross_domain":
            return _FIN_RESEARCH_INTENTS | _AUTO_RESEARCH_INTENTS
        return _FIN_RESEARCH_INTENTS
    return set()


# ── Research worker ─────────────────────────────────────────
def research_worker(state: AgentState, task: dict) -> AgentState:
    """벡터 검색 (pgvector + 메타 필터).

    submodule import 패턴 — 테스트에서 patch('autonexusgraph.tools.retrieve.search_documents')
    또는 patch('autograph.tools.retrieve.search_documents_auto') 가 정상 작동하도록.
    """
    from ..tools.retrieve import search_documents, search_by_metadata, get_chunk

    intent = task.get("intent") or "search"
    args = dict(task.get("args") or {})
    domain = _domain(state)

    # auto / cross_domain 에서 search_documents_auto 류 인텐트 호출.
    if intent in ("search_documents_auto", "search_by_metadata_auto", "get_chunk_auto"):
        try:
            from autograph.tools import retrieve as auto_retrieve
        except ImportError as e:
            _record(state, task, status="failed",
                    result={"error": f"autograph.tools unavailable: {e}"})
            return state
        fn = getattr(auto_retrieve, intent, None)
        if fn is None:
            _record(state, task, status="failed",
                    result={"error": f"no such tool: {intent}"})
            return state
        args.setdefault("query", state.get("question_rewritten") or state.get("question", ""))
        try:
            out = fn(**args)
            _record(state, task, status="done", result=out)
            if isinstance(out, list):
                state.setdefault("evidence_chunks", []).extend(out)
        except Exception as exc:   # noqa: BLE001
            log.warning("[research:auto] %s failed: %s", intent, exc)
            _record(state, task, status="failed", result={"error": str(exc)})
        return state

    # finance (또는 unknown intent) — 기존 동작 보존.
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
    """Neo4j 관계 탐색 (cypher_guard 통과). args 의 intent 가 함수명. 도메인 인식."""
    intent = task.get("intent") or ""
    args = dict(task.get("args") or {})

    allowed = _allowed_intents(state, "graph")
    if intent not in allowed:
        _record(state, task, status="skipped",
                result={"error": f"graph intent 미허용 (domain={_domain(state)}): {intent!r}"})
        return state
    fn = _resolve_tool(state, intent)
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
    """PG 정형 조회. 사전 정의 함수 풀만 (PRD §7.5.10). 도메인 인식."""
    intent = task.get("intent") or ""
    args = dict(task.get("args") or {})

    allowed = _allowed_intents(state, "sql")
    if intent not in allowed:
        _record(state, task, status="skipped",
                result={"error": f"sql intent 미허용 (domain={_domain(state)}): {intent!r}"})
        return state
    fn = _resolve_tool(state, intent)
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
