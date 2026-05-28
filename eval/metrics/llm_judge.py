"""LLM-as-judge — 평가용 LLM 으로 pred vs gold 비교.

사용 시 비용 가드 (memory: feedback-llm-cost-brake) 강제:
- llm.cost.BudgetCheck 통과 후에만 실행
- judge LLM 은 시스템 LLM 과 다른 model 권장 (자기편향 회피)
- 기본 enable=False (P1 baseline 은 EM/F1/Faithfulness 만)
"""

from __future__ import annotations

import json
from typing import Any


_SYSTEM = (
    "당신은 한국 금융 도메인 평가자다. 사용자 질문에 대한 예측 답변(pred)을 "
    "정답(gold)과 비교해 평가한다. JSON 으로만 응답한다."
)

_USER_TEMPLATE = (
    "[질문]\n{question}\n\n"
    "[정답 gold]\n{gold}\n\n"
    "[예측 pred]\n{pred}\n\n"
    "다음 JSON 으로 응답:\n"
    "{{\n"
    '  "correctness": 0.0~1.0,    // pred 가 gold 와 의미 일치 정도\n'
    '  "completeness": 0.0~1.0,   // pred 가 gold 의 핵심 정보를 포함하는 정도\n'
    '  "fluency": 0.0~1.0,        // 답변의 한국어 자연스러움\n'
    '  "rationale": str          // 1~2문장 평가 이유\n'
    "}}"
)

_SCHEMA: dict[str, Any] = {
    "name": "JudgeResult",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["correctness", "completeness", "fluency", "rationale"],
        "properties": {
            "correctness":  {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "completeness": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "fluency":      {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "rationale":    {"type": "string", "maxLength": 400},
        },
    },
}


def llm_judge(
    question: str,
    pred_answer: str,
    gold_answer: str,
    *,
    enable: bool = False,
    judge_role: str = "judge",
) -> dict | None:
    """평가용 LLM 호출. enable=False 면 None.

    호출자가 외부에서 BudgetCheck.review() 이미 통과시켰다고 가정.
    여기서는 budget_aware_client 만 거치고 사용자 명시 hard_limit 적용.
    """
    if not enable:
        return None

    from autonexusgraph.llm.base import get_llm_client
    from autonexusgraph.llm.budget_aware import budget_aware_client
    from autonexusgraph.llm.cost_tracker import BudgetExceeded

    client = budget_aware_client(
        get_llm_client(role=judge_role),
        caller="eval_llm_judge",
    )

    user = _USER_TEMPLATE.format(
        question=question[:500],
        gold=gold_answer[:1500],
        pred=pred_answer[:1500],
    )
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]
    try:
        result = client.chat_json(messages, schema=_SCHEMA, purpose="judge")
    except BudgetExceeded:
        return None
    except Exception:
        return None
    return result
