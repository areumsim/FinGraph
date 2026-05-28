"""멀티턴 coreference rewriter — follow-up 질문의 지시어·생략을 이전 turn 컨텍스트로 해소.

흡수: _legacy/v2/src/agent/query_rewriter.py — 단, FinGraph LLM 추상화에 맞춰
재구성 (`autonexusgraph.llm.LLMClient` 사용).

PRD §7.6.2 — Multi-Turn 동작의 핵심:
- "위에서 답한 회사 중 매출 1조 이상은?" → Planner 가 이전 turn 의 task_results 를 reuse
- "그 중 가장 큰 곳은?" → 그=이전 답변의 회사들
- "방금 그 차트를 산업별로 다시" → 데이터 reuse + 새 그루핑

설계:
- 지시어("그 중", "그것", "이것", "해당", "그런") 또는 짧은 follow-up (10자 미만)이
  있고 직전 대화 컨텍스트가 있으면 LLM 호출. 둘 다 없으면 skip (비용·latency 절감).
- LLM 미가용/응답 실패는 fail-soft — 원본 질문 그대로.
- 환경변수 FINGRAPH_QUERY_REWRITE_ENABLED=false 로 전체 비활성화 가능.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Sequence

logger = logging.getLogger(__name__)

_DEMONSTRATIVE_PATTERNS = (
    r"그\s*중", r"그\s*중에서", r"그것", r"이것", r"해당",
    r"그런(?:데|들)?", r"그\s*후", r"그\s*이후", r"이\s*전(?:의)?",
    r"그러면", r"그렇다면", r"방금", r"앞에서", r"위에서",
    r"이\s*회사", r"그\s*회사", r"그\s*들", r"이\s*사람",
)
_DEMO_RE = re.compile("|".join(_DEMONSTRATIVE_PATTERNS))


def _needs_rewrite(question: str, history: Sequence[dict]) -> bool:
    """지시어/생략이 있고 직전 대화 컨텍스트가 비어있지 않으면 rewrite 필요."""
    if not question or not history:
        return False
    if _DEMO_RE.search(question):
        return True
    # 매우 짧은 follow-up — coreference 가능성 높음
    if len(question.strip()) < 10 and len(history) > 0:
        return True
    return False


def _format_context(history: Sequence[dict]) -> str:
    """history → "Q: ...\\nA: ..." 직렬화. 마지막 3턴만 사용 (토큰 절약)."""
    if not history:
        return ""
    rows: list[str] = []
    # role-based (UI/api에서 들어오는 형태) 또는 q/a key (legacy) 둘 다 지원
    pending_q: str | None = None
    for turn in list(history)[-6:]:
        if not isinstance(turn, dict):
            continue
        if "role" in turn:
            role = turn.get("role")
            content = str(turn.get("content") or "").strip()
            if role == "user" and content:
                pending_q = content
            elif role == "assistant" and content:
                if pending_q:
                    rows.append(f"Q: {pending_q}")
                    pending_q = None
                rows.append(f"A: {content[:300]}")
        else:
            q = str(turn.get("question") or turn.get("q") or "").strip()
            a = str(turn.get("answer") or turn.get("a") or "").strip()
            if q:
                rows.append(f"Q: {q}")
            if a:
                rows.append(f"A: {a[:300]}")
    if pending_q:
        rows.append(f"Q: {pending_q}")
    # 마지막 3턴 (Q+A 6 lines) 만 유지
    return "\n".join(rows[-6:])


_REWRITE_SYSTEM = (
    "당신은 한국어 follow-up 질문의 지시어/생략을 이전 대화 컨텍스트로 풀어주는 도구다. "
    "출력은 한 문장의 재구성된 질문 한 줄. 따옴표·번호 prefix 없음. "
    "이전 대화에 없는 새 정보는 만들지 말 것. "
    "이미 지시어가 없는 자립 질문이면 원문 그대로 출력."
)


def rewrite_query(
    *,
    question: str,
    history: Sequence[dict] = (),
) -> tuple[str, dict]:
    """질의 재구성 — 지시어 없으면 원본 그대로.

    Returns:
        (rewritten_question, audit) — audit: {"called": bool, "reason": str, "input": str,
        "output": str (if called)}
    """
    audit: dict = {"called": False, "reason": "", "input": question}

    if not _needs_rewrite(question, history):
        audit["reason"] = "no_demonstrative_or_history"
        return question, audit
    if os.getenv("FINGRAPH_QUERY_REWRITE_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        audit["reason"] = "env_disabled"
        return question, audit

    context_text = _format_context(history)
    if not context_text:
        audit["reason"] = "empty_context"
        return question, audit

    try:
        from ..llm.base import get_llm_client
        from ..llm.budget_aware import budget_aware_client
        from ..llm.cost_tracker import BudgetExceeded
    except ImportError as exc:   # pragma: no cover
        audit["reason"] = f"import_failed:{exc}"
        return question, audit

    user_msg = (
        f"[이전 대화]\n{context_text}\n\n"
        f"[이번 질문]\n{question}\n\n"
        f"[지시] 위 질문의 지시어/생략을 이전 대화로 풀어 한 줄로 재구성하라."
    )
    try:
        client = budget_aware_client(
            get_llm_client(role="rewriter"),
            caller="agent_rewrite",
            hard_limit=0.05,   # rewrite 는 cheap — turn budget 보다 더 엄격
        )
        resp = client.chat(
            [
                {"role": "system", "content": _REWRITE_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=200,
            purpose="rewrite",
        )
    except BudgetExceeded:
        audit["reason"] = "budget_exceeded"
        return question, audit
    except Exception as exc:   # noqa: BLE001 — fail-soft
        audit["reason"] = f"runtime_error:{type(exc).__name__}"
        logger.warning("rewrite_query 실패 (fail-soft): %s", exc)
        return question, audit

    raw = (resp.content or "").strip()
    if not raw:
        audit["reason"] = "empty_output"
        return question, audit
    # 첫 줄만, 따옴표/번호 prefix 제거
    rewritten = raw.splitlines()[0].strip()
    rewritten = re.sub(r'^[\'"\d\.\s\-]+', "", rewritten).strip()
    if not rewritten or len(rewritten) > 500:
        audit["reason"] = "empty_or_too_long"
        return question, audit

    audit["called"] = True
    audit["reason"] = "rewritten"
    audit["output"] = rewritten
    audit["cost_usd"] = float(getattr(resp.usage, "cost_usd", 0.0) or 0.0)
    logger.info("query rewrite: %r → %r", question, rewritten)
    return rewritten, audit


__all__ = ["rewrite_query"]
