"""SQL + Vector 어댑터 — PG financials + pgvector 본문 검색만 (graph 미사용).

비교용: graph 가 없어도 SQL+Vector 만으로 어디까지 가능한가.
"""

from __future__ import annotations

import re
import time

from .base import AgentAdapter, AgentResponse, Evidence


class SqlVecAdapter(AgentAdapter):
    name = "sql_vec"
    version = "0.1"

    def query(self, question: str, *, domain: str | None = None) -> AgentResponse:  # noqa: ARG002 — sql_vec 는 finance 전용.
        from fingraph.tools.financials import lookup_company, get_revenue, get_operating_income
        from fingraph.tools.retrieve import search_documents
        from fingraph.llm.base import get_llm_client
        from fingraph.llm.budget_aware import budget_aware_client
        from fingraph.llm.cost_tracker import BudgetExceeded

        t0 = time.monotonic()
        # 회사 / 연도 룰 추출
        m = re.search(r"(20\d{2})", question)
        year = int(m.group(1)) if m else None
        targets: list[str] = []
        for w in question.split():
            if len(w) < 2:
                continue
            try:
                hits = lookup_company(w, limit=1)
            except Exception:
                hits = []
            for h in hits:
                if h.get("corp_code") and h["corp_code"] not in targets:
                    targets.append(h["corp_code"])
                    break
            if len(targets) >= 3:
                break

        # SQL — 매출/영업이익
        sql_out: list[dict] = []
        for cc in targets:
            try:
                if year:
                    r = get_revenue(cc, year)
                    if r:
                        sql_out.append({"tool": "get_revenue", "corp_code": cc, "year": year, "result": r})
                    o = get_operating_income(cc, year)
                    if o:
                        sql_out.append({"tool": "get_operating_income", "corp_code": cc, "year": year, "result": o})
            except Exception:
                continue

        # Vector
        try:
            hits = search_documents(question, top_k=5,
                                     corp_code=targets[0] if len(targets) == 1 else None)
        except Exception:
            hits = []

        ctx_sql = "\n".join(f"[{x['tool']}] corp={x['corp_code']} {x['result']}" for x in sql_out)
        ctx_vec = "\n\n".join(
            f"[corp={h.get('corp_code')} sec={h.get('section','')[:30]}]\n{h.get('text','')[:600]}"
            for h in hits[:4]
        )

        try:
            client = budget_aware_client(
                get_llm_client(role="synthesizer"),
                caller="eval_sql_vec_synth",
            )
            resp = client.chat(
                [
                    {"role": "system", "content": "SQL 수치와 본문 근거만 인용해 답하세요."},
                    {"role": "user", "content": f"질문: {question}\n\nSQL:\n{ctx_sql}\n\n본문:\n{ctx_vec}"},
                ],
                temperature=0.0, max_tokens=600,
            )
            answer = resp.content
            cost = resp.usage.cost_usd
            tokens = resp.usage.total_tokens
            refused = False
        except BudgetExceeded:
            answer, cost, tokens, refused = "", 0.0, 0, True

        return AgentResponse(
            answer=answer,
            refused=refused,
            refusal_reason="budget" if refused else "",
            evidence=[
                Evidence(
                    rank=i + 1, chunk_id=h.get("id", 0),
                    corp_code=h.get("corp_code", "") or "",
                    rcept_no=h.get("rcept_no", "") or "",
                    section=h.get("section", "") or "",
                    fiscal_year=h.get("fiscal_year"),
                    source=h.get("source", ""),
                    evidence_text=(h.get("text", "") or "")[:600],
                    score=float(h.get("score") or 0.0),
                )
                for i, h in enumerate(hits)
            ],
            latency_sec=time.monotonic() - t0,
            cost_usd=cost,
            tokens_used=tokens,
            diagnostics={"targets": targets, "year": year, "n_sql": len(sql_out)},
        )
