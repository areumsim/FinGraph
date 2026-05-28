"""Vector-only 어댑터 — pgvector 본문 검색 + LLM 단순 합성.

비교 baseline. graph/hybrid 가 vector-only 대비 얼마나 우위인지 측정용.
"""

from __future__ import annotations

import time

from .base import AgentAdapter, AgentResponse, Evidence


class VectorAdapter(AgentAdapter):
    name = "vector"
    version = "0.1"

    def __init__(self, top_k: int = 8) -> None:
        self.top_k = top_k

    def query(self, question: str, *, domain: str | None = None) -> AgentResponse:  # noqa: ARG002 — vector-only 는 도메인 무관.
        from autonexusgraph.tools.retrieve import search_documents
        from autonexusgraph.llm.base import get_llm_client
        from autonexusgraph.llm.budget_aware import budget_aware_client
        from autonexusgraph.llm.cost_tracker import BudgetExceeded

        t0 = time.monotonic()
        try:
            hits = search_documents(question, top_k=self.top_k)
        except Exception as e:
            return AgentResponse(
                refused=True, refusal_reason=f"retrieve_failed:{e}",
                latency_sec=time.monotonic() - t0,
            )

        if not hits:
            return AgentResponse(
                refused=True, refusal_reason="no_evidence",
                latency_sec=time.monotonic() - t0,
            )

        # 단순 합성 — LLM 한 번. cost_tracker 자동 통합.
        ctx = "\n\n".join(
            f"[corp={h.get('corp_code')} sec={h.get('section','')[:30]} "
            f"score={h.get('score', 0):.3f}]\n{h.get('text','')[:800]}"
            for h in hits[:5]
        )
        try:
            client = budget_aware_client(
                get_llm_client(role="synthesizer"),
                caller="eval_vector_synth",
            )
            resp = client.chat(
                [
                    {"role": "system", "content": "근거 본문만 인용해 한국어로 답하세요."},
                    {"role": "user", "content": f"질문: {question}\n\n근거:\n{ctx}"},
                ],
                temperature=0.0, max_tokens=800,
            )
            answer = resp.content
            cost = resp.usage.cost_usd
            tokens = resp.usage.total_tokens
            refused = False
        except BudgetExceeded:
            answer = ""
            cost = 0.0
            tokens = 0
            refused = True

        return AgentResponse(
            answer=answer,
            refused=refused,
            refusal_reason="budget" if refused else "",
            evidence=[
                Evidence(
                    rank=i + 1, chunk_id=h.get("id", 0),
                    corp_code=h.get("corp_code", ""),
                    rcept_no=h.get("rcept_no", "") or "",
                    section=h.get("section", "") or "",
                    fiscal_year=h.get("fiscal_year"),
                    source=h.get("source", ""),
                    evidence_text=(h.get("text", "") or "")[:600],
                    score=float(h.get("score", 0.0)),
                )
                for i, h in enumerate(hits)
            ],
            latency_sec=time.monotonic() - t0,
            cost_usd=cost,
            tokens_used=tokens,
            question_kind="narrative",
        )
