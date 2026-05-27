"""Validator 노드 + Replan 신호 — PRD §7.5.5.

검증 항목:
1. citation: 답변에 evidence 가 1건도 안 묻어있으면 fail
2. grounding overlap: token overlap < HARD_FAIL 이면 fail (grounding.verify 결과 사용)
3. language: 한국어 비율이 너무 낮으면 fail
4. completeness: 답변이 너무 짧거나 빈 답이면 fail
5. financial number safety: 답변에 등장한 큰 숫자가 도구 결과(tool_results)에서 나온 수치인지 cross-check —
   환각 방지 (PRD §7.3 "재무 수치는 절대 LLM 이 생성하지 않는다")

검증 실패 시 state["validation_status"]="failed", state["validation_issues"] 채움.
graph.run_agent 가 n_replans < MAX 이면 planner 부터 재실행.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .grounding import verify_answer_grounding
from .state import AgentState

log = logging.getLogger(__name__)

# PRD §7.5.5 — replan 무한 루프 방지
MAX_REPLANS = 2

_MIN_ANSWER_LENGTH = 15

# 답변 안의 재무 수치 의심 토큰.
# - 콤마 그룹 2개 이상 (백만 이상) → 분명한 재무 수치
# - leading-digit 1-9 + 7자리 이상 (천만 이상) → 분명한 재무 수치
# leading zero (corp_code 등 식별자) / 4자리 연도는 제외.
# \b 대신 (?<![\d,])(?![\d,]) — '원' 같은 한국어가 \w 로 인식되어 \b 가 비활성화되는 문제 회피.
_BIG_NUMBER_RE = re.compile(
    r"(?<![\d,])(\d{1,3}(?:,\d{3}){2,}|[1-9]\d{6,})(?![\d,])"
)


def _extract_big_numbers(text: str) -> set[str]:
    """답변에서 큰 숫자 토큰 추출. comma 제거 후 비교 용도."""
    if not text:
        return set()
    return {m.group(0).replace(",", "") for m in _BIG_NUMBER_RE.finditer(text)}


def _numbers_from_tool_results(tool_results: list[dict]) -> set[str]:
    """도구 결과 안의 모든 큰 숫자 토큰. 답변 숫자가 이 집합에 있어야 안전."""
    nums: set[str] = set()
    for t in tool_results or []:
        s = str(t.get("result") or "")
        for n in _extract_big_numbers(s):
            nums.add(n)
    return nums


def validator_node(state: AgentState) -> AgentState:
    """답변 합성 후 검증. validation_status / validation_issues 갱신.

    PRD §7.5.5: Validator → failed → Planner replan (count<2).
    """
    from ..safety.language_guard import check_korean

    issues: list[str] = []
    answer = state.get("answer") or ""

    # 1) 답변 길이
    if len(answer.strip()) < _MIN_ANSWER_LENGTH:
        issues.append("answer_too_short")

    # 2) "정보 부족" 자기 신고는 valid — replan 의미 없음
    if "정보 부족" in answer or "데이터 없음" in answer or "정보가 부족" in answer:
        state["validation_status"] = "passed"
        state["validation_issues"] = ["self_reported_insufficient"]
        log.info("[validator] self-reported insufficient — passed without replan")
        return state

    # 3) 한국어 비율 가드
    ok_ko, ratio = check_korean(answer)
    if not ok_ko:
        issues.append(f"language_non_korean_{ratio:.2f}")

    # 4) Grounding (이미 synthesizer 가 채워둘 수 있음)
    grounding = state.get("grounding") or verify_answer_grounding(
        answer=answer,
        evidence_chunks=state.get("evidence_chunks") or [],
    )
    state["grounding"] = grounding
    if not grounding.get("ok"):
        # narrative / multi_hop 류 질문은 evidence 가 핵심. 도구 결과만 있고 evidence 없는
        # 경우(structural 등)는 hard fail 대신 warning.
        kind = state.get("question_kind")
        if kind in ("narrative", "multi_hop") and grounding.get("warnings"):
            issues.extend([f"grounding:{w}" for w in grounding["warnings"]])

    # 5) 재무 수치 환각 가드 — 답변에 등장한 큰 숫자는 도구 결과에 존재해야 함
    answer_nums = _extract_big_numbers(answer)
    if answer_nums:
        safe_nums = _numbers_from_tool_results(state.get("tool_results") or [])
        # evidence chunk 안의 숫자도 OK (본문 인용)
        for ch in state.get("evidence_chunks") or []:
            for n in _extract_big_numbers(str(ch.get("text") or "")):
                safe_nums.add(n)
        hallucinated = answer_nums - safe_nums
        if hallucinated:
            issues.append(f"hallucinated_numbers:{sorted(hallucinated)[:3]}")

    state["validation_issues"] = issues
    if issues:
        # 'low_overlap_but_cited' 같은 soft warning 만 있으면 passed 로 통과
        hard = [i for i in issues if (
            i.startswith("hallucinated_numbers")
            or i.startswith("language_non_korean")
            or i == "answer_too_short"
        )]
        state["validation_status"] = "failed" if hard else "passed"
        if hard:
            log.warning("[validator] failed: %s", hard)
        else:
            log.info("[validator] passed with soft warnings: %s", issues)
    else:
        state["validation_status"] = "passed"
        log.info("[validator] passed clean")
    return state


def should_replan(state: AgentState) -> bool:
    """replan 트리거 — validator failed + n_replans < MAX."""
    if state.get("validation_status") != "failed":
        return False
    n = int(state.get("n_replans") or 0)
    if n >= MAX_REPLANS:
        log.warning("[validator] replan limit (%d) 도달 — 부분 답변 그대로 반환", n)
        return False
    return True


def mark_replan(state: AgentState) -> AgentState:
    """replan 카운터 증가 + 이전 도구 결과·DAG 클리어 (planner 가 새로 채움)."""
    state["n_replans"] = int(state.get("n_replans") or 0) + 1
    state["tool_results"] = []
    state["evidence_chunks"] = []
    state["plan"] = []
    state["tasks"] = []
    state["task_results"] = {}
    state["answer"] = ""
    state["citations"] = []
    state["validation_status"] = "pending"
    log.info("[validator] replan #%d 시작", state["n_replans"])
    return state


__all__ = ["validator_node", "should_replan", "mark_replan", "MAX_REPLANS"]
