"""답변 언어 강제 가드 (흡수: _legacy/v1/src/agent/language_guard.py).

원칙: 모든 최종 답변은 한국어. 고유명사 (DART, GLEIF 등) 원문은 허용하되,
본문·해석·설명은 한국어여야 한다. LLM 이 영어로 응답하거나 한영 혼용으로
응답한 경우 감지 → 재시도 신호.

판정: 측정 대상 문자 = 한글 + 라틴 알파벳. 한글 비율이
FINGRAPH_MIN_KOREAN_RATIO (기본 0.30) 미만이면 fail.
"""

from __future__ import annotations

import os


_MIN_KOREAN_RATIO = float(os.getenv("FINGRAPH_MIN_KOREAN_RATIO", "0.30"))
_MIN_MEASURED_CHARS = int(os.getenv("FINGRAPH_MIN_LANG_CHARS", "20"))


def korean_char_ratio(text: str) -> tuple[float, int]:
    """한글 비율 + 측정에 쓰인 유의미 문자 수 반환.

    유의미 문자 = 한글 + 라틴 알파벳. (한글)/(한글+라틴).
    숫자/공백/구두점 제외.
    """
    if not text:
        return 1.0, 0
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    latin = sum(1 for ch in text if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    denom = hangul + latin
    if denom == 0:
        return 1.0, 0
    return hangul / denom, denom


def check_korean(text: str) -> tuple[bool, float]:
    """답변이 한국어 위주인지. (ok, ratio) 반환.

    측정 문자 수가 너무 적으면 통계적으로 판정 불가 → ok=True (보류).
    """
    ratio, denom = korean_char_ratio(text or "")
    if denom < _MIN_MEASURED_CHARS:
        return True, ratio
    return (ratio >= _MIN_KOREAN_RATIO), ratio


__all__ = ["korean_char_ratio", "check_korean"]
