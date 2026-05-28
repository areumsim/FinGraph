"""답변 근거 검증 (grounding verification).

설계 메모 (이전 v2/agent/grounding_support.py 패턴 흡수):
- evidence 본문 텍스트가 1건이라도 있어야 grounded
- answer 토큰 ∩ evidence 토큰 overlap 비율 — hallucination 신호
- citation marker [1], [2] 있으면 explicit grounding hard signal

코오롱 도메인 anchor/문구 제외. 한국어 paraphrase 흡수형 token 매칭만.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


# 한국어 paraphrase 흡수 — 너무 빡빡하면 false-fail. v2 의 _MIN_* 와 동일 출발선.
_MIN_ANSWER_LENGTH = 10
_MIN_SIGNIFICANT_TOKENS = 5      # 답변 토큰 수가 너무 적으면 overlap 비율 무의미
_MIN_ANSWER_CONFIDENCE = 0.30
_OVERLAP_HARD_FAIL = 0.20         # 이하 + citation 없으면 hard fail
_OVERLAP_WARN = 0.40              # 이하 + bad signal 누적 시 warning

_PUNCT_RE = re.compile(r"[\s　 \.,;:!\?\(\)\[\]\{\}<>\"'`~/\\|@#\$%\^&\*_\-+=]+")
_CITATION_RE = re.compile(r"\[(\d+)\]")


def _extract_tokens(text: str) -> set[str]:
    """공백 split + 한글 char-bigram. eval/metrics/_text_norm 과 같은 알고리즘."""
    if not text:
        return set()
    norm = unicodedata.normalize("NFKC", text).lower()
    out: set[str] = set()
    for w in norm.split():
        w = _PUNCT_RE.sub("", w)
        if not w:
            continue
        out.add(w)
        if any("가" <= ch <= "힣" for ch in w) and len(w) > 2:
            for i in range(len(w) - 1):
                out.add(w[i: i + 2])
    return out


def has_grounded_support(evidence_chunks: list[dict] | tuple[dict, ...]) -> bool:
    """evidence 본문 텍스트가 1건이라도 있어야 grounded.

    chunk_id / corp_code 만 있고 text 비어있는 row 는 grounding 아님.
    """
    for c in evidence_chunks or ():
        if not isinstance(c, dict):
            continue
        if str(c.get("text") or c.get("evidence_text") or "").strip():
            return True
    return False


def count_citations(answer: str) -> int:
    """답변에서 [1], [2] 같은 인용 마커 개수."""
    if not answer:
        return 0
    return len(_CITATION_RE.findall(answer))


def compute_answer_overlap(
    answer: str,
    evidence_chunks: list[dict] | tuple[dict, ...],
) -> tuple[float, int, int]:
    """answer 토큰 ∩ evidence 토큰 / |answer 토큰|. (ratio, matched, total)."""
    answer_tokens = _extract_tokens(answer)
    if not answer_tokens:
        return 0.0, 0, 0
    corpus: set[str] = set()
    for c in evidence_chunks or ():
        if not isinstance(c, dict):
            continue
        corpus |= _extract_tokens(str(c.get("text") or c.get("evidence_text") or ""))
    if not corpus:
        return 0.0, 0, len(answer_tokens)
    matched = len(answer_tokens & corpus)
    # 답변이 너무 짧으면 overlap 무의미 — 1.0 반환 (false-fail 회피)
    if len(answer_tokens) < _MIN_SIGNIFICANT_TOKENS:
        return 1.0, matched, len(answer_tokens)
    return matched / len(answer_tokens), matched, len(answer_tokens)


def verify_answer_grounding(
    *,
    answer: str,
    evidence_chunks: list[dict] | tuple[dict, ...],
    confidence: float | None = None,
    overlap_warn: float = _OVERLAP_WARN,
    overlap_hard_fail: float = _OVERLAP_HARD_FAIL,
) -> dict[str, Any]:
    """LLM 답변 grounding 검증.

    grounding 신호 우선순위 (강함 → 약함):
      1. citation marker [1], [2] — 1건 이상이면 explicit grounding (hard signal)
      2. token overlap — 한국어 paraphrase 흡수 한계로 hard signal X
      3. very low confidence — soft signal

    return dict:
      - ok: bool
      - warnings: list[str]
      - overlap_ratio: float
      - matched_tokens, total_tokens, citation_count
    """
    warnings: list[str] = []

    if not has_grounded_support(evidence_chunks):
        return {
            "ok": False,
            "warnings": ["grounding:no_evidence_text"],
            "overlap_ratio": 0.0,
            "matched_tokens": 0,
            "total_tokens": 0,
            "citation_count": 0,
        }

    if len((answer or "").strip()) < _MIN_ANSWER_LENGTH:
        return {
            "ok": False,
            "warnings": ["grounding:empty_or_too_short_answer"],
            "overlap_ratio": 0.0,
            "matched_tokens": 0,
            "total_tokens": 0,
            "citation_count": 0,
        }

    ratio, matched, total = compute_answer_overlap(answer, evidence_chunks)
    cits = count_citations(answer)

    bad = 0
    if confidence is not None and confidence < _MIN_ANSWER_CONFIDENCE:
        warnings.append("grounding:very_low_confidence")
        bad += 1

    hard_fail = False
    if ratio < overlap_hard_fail:
        if cits == 0:
            hard_fail = True
            warnings.append(f"grounding:low_overlap_{ratio:.2f}_no_citation")
        else:
            warnings.append(f"grounding:low_overlap_{ratio:.2f}_but_cited")
    elif ratio < overlap_warn:
        warnings.append(f"grounding:moderate_overlap_{ratio:.2f}")

    return {
        "ok": not hard_fail,
        "warnings": warnings,
        "overlap_ratio": ratio,
        "matched_tokens": matched,
        "total_tokens": total,
        "citation_count": cits,
        "bad_signals": bad,
    }


__all__ = [
    "has_grounded_support",
    "count_citations",
    "compute_answer_overlap",
    "verify_answer_grounding",
]
