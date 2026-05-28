"""에이전트 (PRD §7.5) — Triage → Planner → Executor → Synthesizer → Validator (→ Replan).

흐름:
1. Triage: prompt safety / coreference rewriter / temporal normalize / 질문 유형 분류 / 회사 식별
2. Planner: 질문 유형 → 도구 호출 계획
3. Executor: tools/ 함수 순차 호출
4. Synthesizer: LLM 답변 합성 + grounding 검증
5. Validator: 환각 / 언어 / 길이 / 수치 안전성 검증 → failed 면 (n_replans<2) replan

진입점:
    from autonexusgraph.agents import run_agent
    state = run_agent("삼성전자 2024년 매출은?")
    print(state["answer"], state["citations"])
"""

from .graph import run_agent, run_agent_resume, run_agent_resume_stream, run_agent_stream
from .state import AgentState
from .validator import MAX_REPLANS

__all__ = [
    "run_agent", "run_agent_stream",
    "run_agent_resume", "run_agent_resume_stream",
    "AgentState", "MAX_REPLANS",
]
