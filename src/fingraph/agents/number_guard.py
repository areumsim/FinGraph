"""Pre-synth number guard — PRD §7.3 "재무 수치는 절대 LLM 이 생성하지 않는다".

Synthesizer 가 LLM 에 보내는 context 안에서 큰 숫자를 **출처와 함께 명시적으로 라벨링**
하고, evidence 본문에 등장한 숫자 외에는 prompt 에 노출되지 않도록 정리한다.
Validator 가 post-hoc 으로 잡지만, 이 guard 는 LLM 이 잘못된 숫자를 답변에 옮기는
근본 입력을 차단한다.

전략:
1. tool_results 의 큰 숫자를 모두 수집 → ``approved_numbers`` 화이트리스트
2. evidence chunks 의 본문에서도 추출 → 동일 화이트리스트 누적
3. system prompt 에 "다음 숫자만 인용 가능: …" 형태로 명시 (10개 cap)
4. evidence text 에서 큰 숫자를 ``[수치:<n>]`` 로 마킹 (LLM 이 인지 쉽게)
5. 미승인 숫자는 evidence text 안에서 ``[검증불가:NUM]`` 으로 치환 → LLM 이 사용 안 하게 유도

큰 숫자 정의 (validator.py 와 동일):
- 콤마 그룹 ≥ 2 (백만 이상) OR leading-digit 1-9 + 7자리 이상 (천만 이상)
- corp_code(8자리 leading 0) / 연도(4자리) / 비율(소수점) 등은 제외
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_BIG_NUMBER_RE = re.compile(
    r"(?<![\d,])(\d{1,3}(?:,\d{3}){2,}|[1-9]\d{6,})(?![\d,])"
)


def _normalize(num: str) -> str:
    """콤마 제거된 정규형."""
    return num.replace(",", "")


def _format_with_commas(n: str) -> str:
    """비교 표시용 — int 면 천 단위 콤마."""
    s = _normalize(n)
    if not s.isdigit():
        return n
    try:
        return f"{int(s):,}"
    except ValueError:
        return n


def collect_approved_numbers(state: dict) -> set[str]:
    """tool_results + evidence_chunks 에 등장한 큰 숫자(정규형) 수집."""
    approved: set[str] = set()
    for t in state.get("tool_results") or []:
        for m in _BIG_NUMBER_RE.finditer(str(t.get("result") or "")):
            approved.add(_normalize(m.group(0)))
    for ch in state.get("evidence_chunks") or []:
        for m in _BIG_NUMBER_RE.finditer(str(ch.get("text") or "")):
            approved.add(_normalize(m.group(0)))
    return approved


def sanitize_evidence_for_synth(
    evidence_chunks: list[dict] | tuple[dict, ...],
    approved: set[str],
    *,
    cap: int = 6,
    text_max: int = 400,
) -> list[dict]:
    """evidence chunks 의 본문에서 미승인 숫자를 [검증불가:NUM] 으로 치환.

    원본 chunks 는 건드리지 않고 새 list 반환. cap / text_max 는 synthesizer
    가 컨텍스트에 사용하는 값과 동일.
    """
    out: list[dict] = []
    for ch in (evidence_chunks or [])[:cap]:
        if not isinstance(ch, dict):
            continue
        text = str(ch.get("text") or "")[:text_max]

        def _repl(m: re.Match[str]) -> str:
            n = _normalize(m.group(0))
            if n in approved:
                return f"[수치:{m.group(0)}]"
            return f"[검증불가:{m.group(0)}]"

        new_text = _BIG_NUMBER_RE.sub(_repl, text)
        new_ch = dict(ch)
        new_ch["text"] = new_text
        out.append(new_ch)
    return out


def format_approved_for_prompt(approved: set[str], *, limit: int = 10) -> str:
    """system prompt 에 박을 화이트리스트 한 줄.

    너무 많으면 limit 만 노출하고 '외 N개' 로 표시.
    """
    if not approved:
        return "(이번 답변에서 인용 가능한 정량 수치 없음 — 수치 인용 금지)"
    sorted_nums = sorted(approved, key=lambda x: (len(x), x))
    head = sorted_nums[:limit]
    formatted = ", ".join(_format_with_commas(n) for n in head)
    extra = len(sorted_nums) - len(head)
    if extra > 0:
        formatted += f", 외 {extra}개"
    return formatted


__all__ = [
    "collect_approved_numbers",
    "sanitize_evidence_for_synth",
    "format_approved_for_prompt",
]
