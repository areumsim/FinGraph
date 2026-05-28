"""Selective LLM 호출 정책 — signal-based 청크 필터.

P3 LLM 호출 비용 50%+ 절감을 위한 사전 필터.
청크 텍스트가 LLM 호출 가치가 있는지(named entity / role / tech 신호) 룰로 판정.

핵심 아이디어:
1. signal-based filter — 회사·관계·시간 signal 없는 청크는 LLM 호출 스킵
2. coverage estimate — 청크 안 signal 대비 이미 매칭된 회사 개수 충분하면 스킵
3. 다중 회사 후보 — 한 청크 안에 회사명 2+ 등장해야 관계 추출 가능성 있음

도메인: **금융 (FinGraph)** — 한국 상장사·DART 사업보고서. 코오롱그룹·BNT_ONTOLOGY
도메인의 키워드는 사용하지 않음.

설계는 v2/pipeline/selective_llm.py 의 알고리즘을 그대로 차용하되, signal token 만
금융 도메인으로 교체.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


# ─── 금융 도메인 signal token 표 ──────────────────────────────
# named: 대문자 약어 (DRAM/SOC/M&A/CEO), 연도(2019~2029), 한글 명사+접미사 패턴
_NAMED_SIGNAL_RE = re.compile(
    r"[A-Z]{2,}|"
    r"20[1-3]\d|"
    r"[가-힣]{2,}(?:전자|화학|반도체|에너지|제약|증권|보험|은행|카드|자동차|중공업|"
    r"건설|물산|상사|텔레콤|디스플레이|바이오|소프트|시스템|네트웍스|홀딩스|"
    r"인터내셔널|코퍼레이션|그룹|컴퍼니|솔루션)"
)

# 관계 signal — 두 회사 간 협력/거래/투자 단서
_ROLE_SIGNAL_TOKENS: tuple[str, ...] = (
    "고객사", "거래처", "공급사", "공급망", "납품",
    "협력사", "파트너", "제휴", "공동개발", "합작",
    "투자", "인수", "합병", "지분", "출자",
    "자회사", "관계회사", "계열사", "모회사", "지배회사",
    "고객", "공급", "조달",
)

# 산업·기술·사업 signal — 사업보고서에서 사업 모델·제품 언급 단서
_TECH_SIGNAL_TOKENS: tuple[str, ...] = (
    "사업", "제품", "서비스", "기술", "시장", "산업",
    "매출", "영업이익", "수익", "분야",
    "AI", "반도체", "디스플레이", "배터리", "전기차", "수소",
    "클라우드", "데이터", "플랫폼", "솔루션",
    "특허", "라이선스", "수출", "수입",
)

# 시간 signal — 시점 표현 (시점 정보 있는 청크 우선)
_TIME_SIGNAL_TOKENS: tuple[str, ...] = (
    "년", "분기", "상반기", "하반기", "회계연도",
)


# 코퍼레이트 명칭에 흔한 접미사 — 회사명 후보 카운트용
_CORP_SUFFIX_RE = re.compile(
    r"(?:주식회사|㈜|\(주\)|Inc\.|Corp\.|Co\.|Ltd\.|"
    r"전자|화학|증권|보험|은행|자동차|중공업|건설|"
    r"홀딩스|그룹|컴퍼니|디스플레이)"
)


@dataclass(frozen=True)
class SelectionDecision:
    """청크별 LLM 호출 여부 + 사유."""
    keep: bool
    reason: str
    signal_count: int
    candidate_companies: int


def has_llm_worthy_signal(text: str) -> bool:
    """청크 텍스트에 LLM 호출 가치가 있는지 (네 종류 signal 중 1개 이상)."""
    if not text:
        return False
    if _NAMED_SIGNAL_RE.search(text):
        return True
    if any(t in text for t in _ROLE_SIGNAL_TOKENS):
        return True
    if any(t in text for t in _TECH_SIGNAL_TOKENS):
        return True
    if any(t in text for t in _TIME_SIGNAL_TOKENS):
        return True
    return False


def count_signals(text: str) -> int:
    """청크 안의 signal 개수 — coverage estimate 분모."""
    if not text:
        return 0
    n = 0
    n += len(_NAMED_SIGNAL_RE.findall(text))
    n += sum(1 for t in _ROLE_SIGNAL_TOKENS if t in text)
    n += sum(1 for t in _TECH_SIGNAL_TOKENS if t in text)
    n += sum(1 for t in _TIME_SIGNAL_TOKENS if t in text)
    return n


def count_corp_candidates(text: str) -> int:
    """청크 안 회사명 후보 수 — 접미사(전자/㈜/Inc 등) 기반 근사.

    2 이상이어야 두 회사 간 관계 후보 (PARTNER_OF / COMPETES_WITH 등) 추출 가치.
    """
    if not text:
        return 0
    return len(_CORP_SUFFIX_RE.findall(text))


def estimate_coverage(text: str, *, matched_entities: int) -> float:
    """기존 매칭 entity 수 / signal 개수. 1.0 이면 이미 충분히 커버됨 → LLM 스킵."""
    n = count_signals(text)
    if n <= 0:
        return 1.0
    return min(1.0, matched_entities / n)


def select_chunk(
    text: str,
    *,
    min_signal_count: int = 2,
    min_corp_candidates: int = 2,
    matched_entities: int = 0,
    skip_if_coverage_above: float = 0.7,
) -> SelectionDecision:
    """단일 청크 → 선택 결정.

    호출 가치 기준:
    1. signal 개수 ≥ min_signal_count
    2. 회사 후보 ≥ min_corp_candidates (관계 추출 전제)
    3. 이미 매칭된 entity 가 signal 의 skip_if_coverage_above 이상 커버하면 skip

    Args:
        matched_entities: P2 (정형 추출) 단계에서 이미 식별한 회사 수.
                          0 이면 coverage 검사 X — 모든 청크 후보.
    """
    sig_n = count_signals(text)
    cand_n = count_corp_candidates(text)

    if sig_n < min_signal_count:
        return SelectionDecision(False, "low_signal", sig_n, cand_n)
    if cand_n < min_corp_candidates:
        return SelectionDecision(False, "few_company_candidates", sig_n, cand_n)
    if matched_entities > 0:
        cov = estimate_coverage(text, matched_entities=matched_entities)
        if cov >= skip_if_coverage_above:
            return SelectionDecision(False, f"already_covered_{cov:.2f}", sig_n, cand_n)
    return SelectionDecision(True, "ok", sig_n, cand_n)


def filter_chunks_by_selectivity(
    chunks: Iterable[dict],
    *,
    text_key: str = "text",
    min_signal_count: int = 2,
    min_corp_candidates: int = 2,
    matched_entities_key: str | None = None,
    skip_if_coverage_above: float = 0.7,
) -> tuple[list[dict], list[dict]]:
    """청크 dict 시퀀스 → (kept, skipped) 분리.

    skipped 는 진단용 dict 리스트 ({chunk: ..., reason: ..., signal_count: ...}).
    LLM 호출 전에 적용해 호출 수 줄임.
    """
    kept: list[dict] = []
    skipped: list[dict] = []
    for ch in chunks:
        text = (ch.get(text_key) or "")
        matched = int(ch.get(matched_entities_key, 0)) if matched_entities_key else 0
        decision = select_chunk(
            text,
            min_signal_count=min_signal_count,
            min_corp_candidates=min_corp_candidates,
            matched_entities=matched,
            skip_if_coverage_above=skip_if_coverage_above,
        )
        if decision.keep:
            kept.append(ch)
        else:
            skipped.append({
                "chunk_id": ch.get("id"),
                "reason": decision.reason,
                "signal_count": decision.signal_count,
                "candidate_companies": decision.candidate_companies,
            })
    return kept, skipped


__all__ = [
    "SelectionDecision",
    "has_llm_worthy_signal",
    "count_signals",
    "count_corp_candidates",
    "estimate_coverage",
    "select_chunk",
    "filter_chunks_by_selectivity",
]
