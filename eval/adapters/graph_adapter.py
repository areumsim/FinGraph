"""Graph-only 어댑터 — Neo4j 그래프 탐색만, vector 미사용.

비교용: vector 가 없으면 멀티홉 추론에서 얼마나 못하는지 측정.
"""

from __future__ import annotations

import time

from .base import AgentAdapter, AgentResponse


class GraphAdapter(AgentAdapter):
    name = "graph"
    version = "0.1"

    def query(self, question: str, *, domain: str | None = None) -> AgentResponse:  # noqa: ARG002 — graph-only 는 finance 그래프만 본다.
        from autonexusgraph.agents.policy import classify_question
        from autonexusgraph.tools.graph import lookup_company, list_subsidiaries, get_executives, get_major_shareholders
        from autonexusgraph.llm.base import get_llm_client
        from autonexusgraph.llm.budget_aware import budget_aware_client
        from autonexusgraph.llm.cost_tracker import BudgetExceeded

        t0 = time.monotonic()
        kind = classify_question(question)

        # 회사 식별
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

        if not targets:
            return AgentResponse(
                refused=True, refusal_reason="no_company_identified",
                latency_sec=time.monotonic() - t0, question_kind=kind,
            )

        # 도구 호출 — vector 안 씀
        graph_out: list[dict] = []
        for cc in targets:
            graph_out.append({"tool": "list_subsidiaries",
                              "result": list_subsidiaries(cc, limit=15)})
            graph_out.append({"tool": "get_executives",
                              "result": get_executives(cc, limit=20)})
            graph_out.append({"tool": "get_major_shareholders",
                              "result": get_major_shareholders(cc, limit=10)})

        # 합성 — LLM
        ctx = "\n".join(
            f"[{g['tool']}] {str(g['result'])[:500]}" for g in graph_out
        )
        try:
            client = budget_aware_client(
                get_llm_client(role="synthesizer"),
                caller="eval_graph_synth",
            )
            resp = client.chat(
                [
                    {"role": "system", "content": "그래프 출력만 근거로 한국어 답변."},
                    {"role": "user", "content": f"질문: {question}\n\n그래프 결과:\n{ctx}"},
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
            question_kind=kind,
            latency_sec=time.monotonic() - t0,
            cost_usd=cost,
            tokens_used=tokens,
            diagnostics={"targets": targets, "n_tool_calls": len(graph_out)},
        )
