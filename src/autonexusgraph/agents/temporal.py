"""한국어 상대 시간 표현 정규화 — "작년"/"재작년"/"올해"/"최근 N년" → 절대 연도/범위.

흡수: _legacy/v2/src/agent/temporal_normalizer.py (PRD §6.3 — v1/v2 자산 흡수 원칙).

triage / planner 전에 호출하면 LLM 단계가 시점을 명시적 연도로 받게 되어
financials.get_revenue(year=...) 같은 정량 도구 호출 정확도가 올라간다.

reference_date: 인자 > FINGRAPH_REFERENCE_DATE env > date.today().

매핑:
  - 작년 / 지난해              → year-1
  - 재작년 / 그제년             → year-2
  - 올해 / 금년 / 이번해         → year
  - 내년 / 명년                → year+1
  - 최근 N년 / 최근 N개년       → range [year-N+1, year]
  - 지난 N년 / 직전 N년         → range [year-N, year-1]
  - 향후 N년 / 앞으로 N년       → range [year+1, year+N]
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


def _resolve_reference_date(reference_date: Optional[date] = None) -> date:
    if reference_date is not None:
        return reference_date
    raw = os.getenv("FINGRAPH_REFERENCE_DATE", "").strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            logger.warning("FINGRAPH_REFERENCE_DATE 파싱 실패 — today() 사용: %s", raw)
    return date.today()


# 길이 내림차순 — 'N년' 단독이 'N년도'를 부분 매칭하지 않게.
_REL_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"재작년"), -2),
    (re.compile(r"그제년"), -2),
    (re.compile(r"작년"), -1),
    (re.compile(r"지난해"), -1),
    (re.compile(r"올해"), 0),
    (re.compile(r"금년"), 0),
    (re.compile(r"이번해"), 0),
    (re.compile(r"내년"), 1),
    (re.compile(r"명년"), 1),
]

_RANGE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"최근\s*(\d{1,2})\s*(?:개년|년)"), "recent"),
    (re.compile(r"지난\s*(\d{1,2})\s*(?:개년|년)"), "past"),
    (re.compile(r"직전\s*(\d{1,2})\s*(?:개년|년)"), "past"),
    (re.compile(r"향후\s*(\d{1,2})\s*(?:개년|년)"), "future"),
    (re.compile(r"앞으로\s*(\d{1,2})\s*(?:개년|년)"), "future"),
]


def normalize_temporal_terms(
    question: str,
    *,
    reference_date: Optional[date] = None,
) -> tuple[str, dict]:
    """질문 안의 상대 시간 표현을 절대 연도로 치환.

    Returns:
        (rewritten, audit). 매칭 없으면 audit["applied"] 빈 리스트, rewritten==원문.
    """
    audit: dict = {
        "applied": [],
        "year_from": None,
        "year_to": None,
        "reference_date": "",
    }
    if not question:
        return question, audit
    ref = _resolve_reference_date(reference_date)
    audit["reference_date"] = ref.isoformat()
    year = ref.year

    rewritten = question

    for pat, delta in _REL_PATTERNS:
        def _replace(match: re.Match[str], _y: int = year + delta) -> str:
            audit["applied"].append((match.group(0), f"{_y}년"))
            return f"{_y}년"
        new_rewritten, n = pat.subn(_replace, rewritten)
        if n > 0:
            rewritten = new_rewritten
            if audit["year_from"] is None or (year + delta) < audit["year_from"]:
                audit["year_from"] = year + delta
            if audit["year_to"] is None or (year + delta) > audit["year_to"]:
                audit["year_to"] = year + delta

    for pat, kind in _RANGE_PATTERNS:
        for m in list(pat.finditer(rewritten)):
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if n <= 0 or n > 50:
                continue
            if kind == "recent":
                from_y, to_y = year - n + 1, year
            elif kind == "past":
                from_y, to_y = year - n, year - 1
            else:
                from_y, to_y = year + 1, year + n
            replacement = f"{from_y}년부터 {to_y}년까지"
            rewritten = rewritten.replace(m.group(0), replacement, 1)
            audit["applied"].append((m.group(0), replacement))
            if audit["year_from"] is None or from_y < audit["year_from"]:
                audit["year_from"] = from_y
            if audit["year_to"] is None or to_y > audit["year_to"]:
                audit["year_to"] = to_y

    if audit["applied"]:
        logger.info("temporal normalize: %r → %r (audit=%s)",
                    question, rewritten, audit)
    return rewritten, audit


def extract_year_hint(question: str, *, reference_date: Optional[date] = None) -> Optional[int]:
    """질문에서 단일 연도 힌트 추출 (planner.get_revenue year= 용).

    우선순위: (1) 정규화가 적용된 경우 audit.year_to ← 사용자의 의도가 명백한 상대 시점.
              (2) 정규화 미적용이면 질문 본문의 명시적 4자리 연도.
    """
    rewritten, audit = normalize_temporal_terms(question, reference_date=reference_date)
    if audit.get("applied"):
        return audit.get("year_to")
    m = re.search(r"(19|20)\d{2}", rewritten)
    if m:
        return int(m.group(0))
    return None


__all__ = ["normalize_temporal_terms", "extract_year_hint"]
