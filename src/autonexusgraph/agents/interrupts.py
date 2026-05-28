"""Human-in-the-Loop interrupt 페이로드 — PRD §7.5.6.

LangGraph ``interrupt()`` 를 통해 graph 가 멈추고 client 가 응답할 때까지 대기.
응답이 들어오면 graph 가 같은 thread 의 checkpoint 부터 재개한다.

사용 시점 (PRD):
1. Clarification — 모호한 회사명 ("삼성" → 삼성전자/SDS/SDI...) — Triage 단계
2. Cost approval — Planner 산출 비용이 한도 초과
3. Sensitive decision — 외부 보고용 / 민감 답변 — Synthesizer 직전

이번 PR 는 (1) clarification 만 구현. (2)(3) 은 동일 helper 로 후속.

설계:
- langgraph 1.x 의 ``langgraph.types.interrupt`` import 우선
- langgraph 미설치 / 폴백 체인에서는 InterruptUnavailable 예외 → 호출부가 우아한
  다운그레이드 (1순위 후보 자동 선택 + state.fallback_used 경고).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, TypedDict

logger = logging.getLogger(__name__)


InterruptKind = Literal[
    "company_clarification",
    "cost_approval",
    "sensitive_decision",
]


class InterruptPayload(TypedDict, total=False):
    """interrupt 호출 시 client 에게 yield 되는 페이로드."""
    kind: InterruptKind
    prompt: str                      # 사용자에게 보일 한국어 질문
    candidates: list[dict]           # company_clarification 용 후보 목록
    estimated_cost_usd: float        # cost_approval 용 예상 비용
    plan_summary: str                # cost_approval 용 plan 요약
    answer_preview: str              # sensitive_decision 용 답변 미리보기
    thread_id: str                   # resume 시 식별용


class InterruptUnavailable(RuntimeError):
    """langgraph interrupt API 사용 불가 — 호출부가 폴백 처리."""


def request_interrupt(payload: InterruptPayload) -> Any:
    """LangGraph interrupt 호출. 응답을 반환 (resume 값).

    langgraph 미설치 / fallback chain → InterruptUnavailable raise.
    """
    try:
        from langgraph.types import interrupt   # type: ignore[import-not-found]
    except ImportError:
        try:
            from langgraph.graph import interrupt   # type: ignore[attr-defined]
        except ImportError as exc:
            raise InterruptUnavailable("langgraph interrupt API 미사용 (폴백 환경)") from exc
    logger.info("[interrupt] kind=%s prompt=%r", payload.get("kind"),
                str(payload.get("prompt", ""))[:80])
    return interrupt(dict(payload))


# ── Clarification — 모호한 회사명 ──────────────────────────
def is_ambiguous_company(candidates: list[dict],
                         *, max_margin: float = 0.10,
                         min_n: int = 2) -> bool:
    """후보 N>=min_n + 1·2위 score 차이 < max_margin 이면 모호.

    score 가 없으면 1·2위 이름이 다르고 결합 score 동률로 가정.
    """
    if not candidates or len(candidates) < min_n:
        return False
    scores = [float(c.get("score") or 0.0) for c in candidates[:2]]
    if scores[0] == 0.0 and scores[1] == 0.0:
        # score 없음 — 후보가 여럿이면 모호로 간주
        return True
    margin = scores[0] - scores[1]
    return margin < max_margin * max(scores[0], 1.0)


def make_clarification_payload(
    query: str,
    candidates: list[dict],
    *, thread_id: str = "",
    limit: int = 5,
) -> InterruptPayload:
    """후보 목록을 사용자가 선택할 수 있는 형태로 변환."""
    short = []
    for c in candidates[:limit]:
        short.append({
            "corp_code": c.get("corp_code"),
            "name": c.get("name") or c.get("corp_name") or "",
            "stock_code": c.get("stock_code"),
            "market": c.get("market"),
            "score": c.get("score"),
        })
    return {
        "kind": "company_clarification",
        "prompt": f'"{query}" 와 일치하는 회사가 여러 곳입니다. 어떤 곳을 의미하시나요?',
        "candidates": short,
        "thread_id": thread_id,
    }


def coerce_clarification_response(
    response: Any,
    candidates: list[dict],
) -> str | None:
    """resume 값을 corp_code 로 정규화. dict / int / str 모두 수용.

    return: 선택된 corp_code 또는 None (인식 불가)
    """
    if not response:
        return None
    if isinstance(response, dict):
        cc = response.get("corp_code")
        if isinstance(cc, str) and cc:
            return cc
        idx = response.get("index")
        if isinstance(idx, int) and 0 <= idx < len(candidates):
            return str(candidates[idx].get("corp_code") or "")
    if isinstance(response, int) and 0 <= response < len(candidates):
        return str(candidates[response].get("corp_code") or "")
    if isinstance(response, str):
        # corp_code 8자리 직접 입력
        if response.isdigit() and len(response) == 8:
            return response
        # 이름으로 매칭 — 후보 중 정확히 일치
        for c in candidates:
            if (c.get("name") or "") == response or (c.get("corp_name") or "") == response:
                return str(c.get("corp_code") or "")
    return None


# ── Cost approval ─────────────────────────────────────────
def make_cost_approval_payload(
    *,
    estimated_cost_usd: float,
    plan_summary: str,
    thread_id: str = "",
) -> InterruptPayload:
    """planner 비용 추정이 한도 초과 시 발동."""
    return {
        "kind": "cost_approval",
        "prompt": f"이 질문 처리에 예상 ${estimated_cost_usd:.4f} 소요됩니다. 진행할까요?",
        "estimated_cost_usd": float(estimated_cost_usd),
        "plan_summary": plan_summary,
        "thread_id": thread_id,
    }


def coerce_cost_response(response: Any) -> bool:
    """resume 값을 승인/거절 boolean 으로 정규화.

    True/False / "y"·"yes"·"ok"·"approve" / dict{"approved": bool}.
    인식 불가 → False (보수적: 비용 발생 거부).
    """
    if response is True:
        return True
    if response is False or response is None:
        return False
    if isinstance(response, dict):
        v = response.get("approved")
        if isinstance(v, bool):
            return v
    if isinstance(response, str):
        s = response.strip().lower()
        if s in ("y", "yes", "ok", "approve", "approved", "true", "1", "승인"):
            return True
        if s in ("n", "no", "deny", "reject", "false", "0", "거절", "취소"):
            return False
    return False


__all__ = [
    "InterruptKind",
    "InterruptPayload",
    "InterruptUnavailable",
    "request_interrupt",
    "is_ambiguous_company",
    "make_clarification_payload",
    "coerce_clarification_response",
    "make_cost_approval_payload",
    "coerce_cost_response",
]
