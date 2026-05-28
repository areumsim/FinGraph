"""에이전트 StateGraph 상태 정의.

LangGraph 도입 시 그대로 StateGraph[AgentState] 로 사용 가능한 형태.
현재는 langgraph 미설치 → graph.py 가 단순 함수 체인으로 동작.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


QuestionKind = Literal["factual", "narrative", "structural", "multi_hop", "unknown"]

# PRD §7.5.2 / §7.5.3 — Supervisor 가 라우팅하는 worker agent 타입.
AgentName = Literal["research", "graph", "sql", "calculator"]
TaskStatus = Literal["pending", "running", "done", "failed", "skipped"]


class AgentState(TypedDict, total=False):
    """conversation 한 turn 의 누적 상태."""

    # 입력
    thread_id: str
    question: str
    history: list[dict]               # 이전 messages — multi-turn 컨텍스트
    domain: str                       # 'finance' | 'auto' | 'cross_domain' (default 'finance')
    target_vehicles: list[int]        # AutoGraph variant_id 목록 (auto/cross_domain)
    target_models: list[int]          # AutoGraph model_id 목록
    target_makes: list[str]           # AutoGraph make 이름 (raw — lookup_vehicle 결과 보조)

    # 전처리 (rewriter / temporal 결과)
    question_rewritten: str           # coreference 해소 + 시점 정규화된 query
    temporal_audit: dict              # {applied, year_from, year_to, reference_date}
    rewrite_audit: dict               # {called, reason, output}
    safety_signals: list[str]         # prompt injection 감지 토큰 (있으면 telemetry)

    # Triage / Planner 결정
    question_kind: QuestionKind
    target_companies: list[str]       # corp_code 목록 (lookup_company 결과)
    session_carryover: bool           # 이번 turn 의 target 이 이전 세션에서 borrow 됐는지
    plan: list[dict]                  # legacy flat plan — tasks 가 비어 있을 때 폴백 executor 가 사용
    tasks: list[dict]                 # DAG (PRD §7.5.3) — 각 항목:
                                      #   {"id": str, "agent": AgentName, "intent": str,
                                      #    "args": dict, "depends_on": list[str],
                                      #    "status": TaskStatus, "result": Any}
    task_results: dict                # task_id → result (append-only, supervisor 가 채움)

    # 실행 결과
    tool_results: list[dict]          # 도구별 출력 묶음 (legacy + worker 도 push)
    evidence_chunks: list[dict]       # search_documents 결과
    graph_subgraph: dict | None       # 시각화용
    fallback_used: bool               # 빈 결과 회복으로 fallback search_documents 호출됐는지

    # 합성
    answer: str
    citations: list[dict]             # [{"chunk_id": ..., "corp_code": ..., "section": ...}]
    visualizations: list[dict]        # [{"kind": "subgraph", ...}, {"kind": "chart", ...}]

    # Validation (PRD §7.5.5)
    validation_status: str            # 'pending' | 'passed' | 'failed'
    validation_issues: list[str]      # 검증 실패 사유들
    grounding: dict                   # verify_answer_grounding 결과

    # Human-in-the-Loop (PRD §7.5.6)
    pending_interrupt: dict           # 발동된 interrupt 페이로드 (UI/SSE 로 노출)
    interrupt_response: Any           # client 가 보낸 resume 값
    interrupt_handled: bool           # graph 가 응답 처리 완료 신호

    # 메타·비용
    llm_usage_usd: float
    n_replans: int
    aborted_reason: str | None
