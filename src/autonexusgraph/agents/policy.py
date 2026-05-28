"""에이전트 라우팅 정책 + cost guard.

원칙 (PRD §7.1):
- 단순 사실 (회사·연도·지표) → financials tool 직접
- 의미·서술 → retrieve.search_documents
- 관계·구조 → graph tools
- 멀티홉 → 다중 도구 조합

cost guard:
- 한 turn 의 누적 비용이 AGENT_TURN_BUDGET_USD 초과 시 즉시 답변 단계로 점프.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..config import get_settings
from .state import AgentState, QuestionKind


# 룰 기반 1차 분류 — 빠르고 LLM 호출 없음. 모호하면 'unknown' → planner LLM 이 재분류.
RE_YEAR        = re.compile(r"(19|20)\d{2}\s*년?")
KW_FINANCIAL   = ("매출", "영업이익", "순이익", "자산", "부채", "ROE", "ROA", "PER", "PBR")
KW_STRUCTURAL  = ("자회사", "임원", "대표", "주주", "지분", "계열사", "기업집단", "모회사")
KW_NARRATIVE   = ("위험", "전략", "전망", "사업 개요", "비즈니스 모델", "주요사항", "ESG")
KW_MULTIHOP    = ("중에", "들의", "함께", "동시에", "vs", "비교", "합산", "총합")


def classify_question(question: str) -> QuestionKind:
    """질문 유형 룰 분류 — LLM 호출 X. 모호하면 unknown."""
    q = question or ""
    has_year = bool(RE_YEAR.search(q))
    f = any(k in q for k in KW_FINANCIAL)
    s = any(k in q for k in KW_STRUCTURAL)
    n = any(k in q for k in KW_NARRATIVE)
    m = any(k in q for k in KW_MULTIHOP)

    # 우선순위: multi_hop > structural > factual > narrative
    if m and (f or s):
        return "multi_hop"
    if s and not n:
        return "structural"
    if f and has_year:
        return "factual"
    if n:
        return "narrative"
    if s:
        return "structural"
    if f:
        return "factual"
    return "unknown"


def turn_budget_remaining(state: AgentState) -> float:
    """이 turn 의 남은 예산 (USD). 0 또는 음수면 중단 신호.

    도메인별 override 가 있으면 그것을 사용 — auto/cross_domain 분리 추적.
    """
    from ..config import turn_budget_for_domain
    used = float(state.get("llm_usage_usd") or 0.0)
    return turn_budget_for_domain(state.get("domain")) - used


def turn_budget_exceeded(state: AgentState) -> bool:
    return turn_budget_remaining(state) <= 0.0


def select_tools(kind: QuestionKind) -> list[str]:
    """질문 유형 → 권장 도구 목록 (Planner 가 ground 잡는 용도)."""
    if kind == "factual":
        return ["lookup_company", "get_revenue", "get_operating_income"]
    if kind == "structural":
        return ["lookup_company", "list_subsidiaries", "get_executives",
                "get_major_shareholders", "get_subgraph"]
    if kind == "narrative":
        return ["lookup_company", "search_documents"]
    if kind == "multi_hop":
        return ["lookup_company", "list_subsidiaries", "get_companies_of_person",
                "find_paths", "get_revenue", "search_documents"]
    # unknown → 안전한 default
    return ["lookup_company", "search_documents"]


__all__ = [
    "classify_question", "turn_budget_remaining", "turn_budget_exceeded",
    "select_tools",
]
