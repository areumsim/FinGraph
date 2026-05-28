"""에이전트 turn 비용 estimator — Planner 산출 후 cost approval 가드용 (PRD §7.5.6).

추정 근거:
- Workers (Research/Graph/SQL/Calculator): LLM 비호출 → 0
- Rewriter (Triage): LLM 호출 가능 — history 있고 지시어 있을 때만. 단가 매우 낮음 (200 tok 한도)
- Title 생성: 첫 turn 한정, 30 tok — 무시 가능
- Synthesizer: LLM 핵심 비용. context = question + tool_results + evidence + system prompt.
  Replan 발생 시 곱하기.

추정은 보수적 (over-estimate). 실제 비용은 ``cost_tracker`` 가 누적 집계.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import get_settings
from ..llm.cost import _resolve_pricing
from .state import AgentState


# 토큰 추정 — 한국어 1글자 ≈ 1.5 tokens (BPE 평균). 영어 4글자 ≈ 1 token.
# 보수적: 글자 수 / 2 (over-estimate)
def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 2)


@dataclass(frozen=True)
class TurnCostEstimate:
    model: str
    expected_input_tokens: int
    expected_output_tokens: int
    base_cost_usd: float
    replan_factor: int               # 1 + MAX_REPLANS (보수적)
    estimated_cost_usd: float        # base_cost_usd × replan_factor
    breakdown: dict[str, float]      # {synth, rewriter, title, …}

    def format(self) -> str:
        return (
            f"[TURN COST EST] {self.model} ≈ "
            f"in≤{self.expected_input_tokens:,} / out≤{self.expected_output_tokens:,} tok, "
            f"base=${self.base_cost_usd:.4f} × replan{self.replan_factor} "
            f"⇒ max ${self.estimated_cost_usd:.4f}"
        )


def estimate_turn_cost(state: AgentState) -> TurnCostEstimate:
    """Planner 산출 후 호출 — Synthesizer 비용 + replan 곱하기.

    Synthesizer 모델은 settings.llm_model_synthesizer 우선, 없으면 settings.llm_model.
    """
    s = get_settings()
    model = getattr(s, "llm_model_synthesizer", "") or s.llm_model
    in_per_1m, out_per_1m = _resolve_pricing(model)

    # ── Synthesizer 입력 토큰 추정 ─────────────────────────
    # 시스템 프롬프트 (≈200 tok) + 질문 + 도구 결과 + evidence
    q = state.get("question_rewritten") or state.get("question", "")
    sys_tokens = 200
    q_tokens = _approx_tokens(q)

    tool_results = state.get("tool_results") or []
    tool_str_tokens = 0
    for t in tool_results:
        tool_str_tokens += _approx_tokens(str(t.get("result"))[:1000])
    # 추가: planner 가 만든 tasks 의 args 크기도 영향 (다단 호출 시)
    n_tasks = len(state.get("tasks") or [])
    task_tokens = n_tasks * 50   # 보수적 — task 하나당 ~50 tok output 으로 채워질 것

    evidence = state.get("evidence_chunks") or []
    ev_tokens = 0
    for ch in evidence[:6]:   # synth context 가 최대 6개 (nodes.py)
        ev_tokens += _approx_tokens(str(ch.get("text") or "")[:400])

    expected_input = sys_tokens + q_tokens + tool_str_tokens + task_tokens + ev_tokens
    expected_output = 1200   # synth max_tokens 기본

    synth_cost = (
        expected_input * in_per_1m / 1_000_000
        + expected_output * out_per_1m / 1_000_000
    )

    # ── Rewriter (있을 때만) ───────────────────────────────
    rewriter_cost = 0.0
    if state.get("history") and state.get("rewrite_audit", {}).get("called"):
        # rewriter 는 ~600 in / 200 out 정도 (코드의 max_tokens=200)
        rewriter_cost = (600 * in_per_1m + 200 * out_per_1m) / 1_000_000

    # ── Title (첫 turn 한정, 매우 적음) ────────────────────
    title_cost = 0.0
    if not state.get("history"):
        title_cost = (50 * in_per_1m + 30 * out_per_1m) / 1_000_000

    base = synth_cost + rewriter_cost + title_cost

    # Replan 곱하기 — Validator 가 실패하면 최대 MAX_REPLANS+1 회 synth 호출.
    # 보수적으로 (1 + max_replans) 곱
    max_replans = int(getattr(s, "agent_max_replan", 2))
    replan_factor = max_replans + 1
    total = base * replan_factor

    return TurnCostEstimate(
        model=model,
        expected_input_tokens=int(expected_input),
        expected_output_tokens=int(expected_output * replan_factor),
        base_cost_usd=base,
        replan_factor=replan_factor,
        estimated_cost_usd=total,
        breakdown={
            "synth_per_turn": synth_cost,
            "rewriter": rewriter_cost,
            "title": title_cost,
        },
    )


def needs_cost_approval(state: AgentState) -> tuple[bool, TurnCostEstimate]:
    """추정 비용이 LLM_COST_AUTO_APPROVE_USD 초과면 (True, est) 반환."""
    est = estimate_turn_cost(state)
    s = get_settings()
    threshold = float(getattr(s, "llm_cost_auto_approve_usd", 0.50))
    return (est.estimated_cost_usd > threshold, est)


__all__ = [
    "TurnCostEstimate",
    "estimate_turn_cost",
    "needs_cost_approval",
]
