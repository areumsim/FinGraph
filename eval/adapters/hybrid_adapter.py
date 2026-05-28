"""Hybrid 어댑터 — FinGraph 의 본 agent (Triage/Planner/Executor/Synthesizer).

PRD §2.2 의 목표: vector-only 대비 multi-hop +30%p 우위 입증.
"""

from __future__ import annotations

import time

from .base import AgentAdapter, AgentResponse, Evidence


class HybridAdapter(AgentAdapter):
    name = "hybrid"
    version = "0.1"

    def query(self, question: str, *,
              domain: str | None = None) -> AgentResponse:
        from autonexusgraph.agents import run_agent

        t0 = time.monotonic()
        try:
            state = run_agent(question, domain=domain)
        except Exception as e:
            return AgentResponse(
                refused=True, refusal_reason=f"agent_failed:{e}",
                latency_sec=time.monotonic() - t0,
            )

        evidence = [
            Evidence(
                rank=i + 1, chunk_id=c.get("chunk_id", 0),
                corp_code=c.get("corp_code", "") or "",
                rcept_no=c.get("rcept_no", "") or "",
                section=c.get("section", "") or "",
                fiscal_year=c.get("fiscal_year"),
                source="",
                evidence_text="",
                score=float(c.get("score") or 0.0),
            )
            for i, c in enumerate(state.get("citations") or [])
        ]

        return AgentResponse(
            answer=state.get("answer", ""),
            refused=bool(state.get("aborted_reason")),
            refusal_reason=state.get("aborted_reason") or "",
            evidence=evidence,
            question_kind=state.get("question_kind", ""),
            latency_sec=time.monotonic() - t0,
            cost_usd=float(state.get("llm_usage_usd") or 0.0),
            diagnostics={
                "targets": state.get("target_companies") or [],
                "n_tool_results": len(state.get("tool_results") or []),
            },
        )
