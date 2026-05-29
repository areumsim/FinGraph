"""eval/metrics 공용 markdown 렌더 helper.

여러 metric 모듈 (data_coverage / bom_coverage / bridge_quality / core_diff /
edge_meta_completeness) 이 동일한 ✅/❌ mark 와 % 표기 헬퍼를 사용한다.
한 곳에서 정의해 일관된 표기.
"""

from __future__ import annotations


def mark(ok: bool, *, true_glyph: str = "✅", false_glyph: str = "❌") -> str:
    return true_glyph if ok else false_glyph


def pct(value: float, *, decimals: int = 1) -> str:
    """0.0~1.0 비율을 'XX.X%' 로 — None/0/누락 안전."""
    if value is None:
        return "—"
    return f"{value * 100:.{decimals}f}%"


def ratio(num: int | None, den: int | None) -> str:
    """'num/den = pct' 형식. den=0 → 'num/0'."""
    n = int(num or 0)
    d = int(den or 0)
    if d == 0:
        return f"{n}/0"
    return f"{n}/{d} = {pct(n / d)}"


__all__ = ["mark", "pct", "ratio"]
