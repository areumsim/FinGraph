"""load_auto_safety 단위 테스트 — DB 없이 파서·매핑만 검증.

응답 파싱 / value_text vs value_num 분기 / 잘못된 입력 graceful.
"""

from __future__ import annotations

from autograph.loaders.load_auto_safety import (
    _RATING_MAP,
    _parse_pct,
    _parse_star,
)


# ── _parse_star ─────────────────────────────────────────────
def test_parse_star_valid():
    assert _parse_star("5") == 5.0
    assert _parse_star("4") == 4.0
    assert _parse_star("3.5") == 3.5
    assert _parse_star(" 5 ") == 5.0


def test_parse_star_invalid_returns_none():
    assert _parse_star(None) is None
    assert _parse_star("") is None
    assert _parse_star("Not Rated") is None
    assert _parse_star("N/A") is None


def test_parse_star_out_of_range():
    assert _parse_star("6") is None
    assert _parse_star("-1") is None


# ── _parse_pct ──────────────────────────────────────────────
def test_parse_pct_valid():
    assert _parse_pct("12.34%") == 12.34
    assert _parse_pct("8.5%") == 8.5
    assert _parse_pct("0%") == 0.0
    # 일부 응답은 % 없이 숫자만.
    assert _parse_pct("15.2") == 15.2


def test_parse_pct_invalid_returns_none():
    assert _parse_pct(None) is None
    assert _parse_pct("") is None
    assert _parse_pct("N/A") is None
    assert _parse_pct("not a number") is None


# ── _RATING_MAP 구조 정합성 ─────────────────────────────────
def test_rating_map_covers_core_ncap_fields():
    """핵심 NCAP 필드 13종이 모두 매핑 등록되어 있는지."""
    required_fields = {
        "OverallRating",
        "OverallFrontCrashRating",
        "FrontCrashDriversideRating",
        "FrontCrashPassengersideRating",
        "OverallSideCrashRating",
        "SideCrashDriversideRating",
        "SideCrashPassengersideRating",
        "SidePoleCrashRating",
        "RolloverRating",
        "RolloverPossibility",
        "NHTSAElectronicStabilityControl",
        "NHTSAForwardCollisionWarning",
        "NHTSALaneDepartureWarning",
    }
    assert required_fields <= set(_RATING_MAP.keys())


def test_rating_map_value_types():
    """value_type 'star' | 'pct' | 'text' 만 허용."""
    for field, (measure_key, unit, vtype) in _RATING_MAP.items():
        assert vtype in ("star", "pct", "text"), f"{field}: {vtype}"
        # measure_key 컨벤션 — safety.* 접두사.
        assert measure_key.startswith("safety."), f"{field}: {measure_key}"


def test_star_fields_have_star_unit():
    for field, (measure_key, unit, vtype) in _RATING_MAP.items():
        if vtype == "star":
            assert unit == "star", f"{field}: {unit}"
        elif vtype == "pct":
            assert unit == "percent", f"{field}: {unit}"


# ── ingestion / loader 모듈 import smoke ────────────────────
def test_safety_modules_importable():
    """ingestion + loader 둘 다 import 가능 (cypher_guard 등 의존 깨지지 않음)."""
    import autograph.ingestion.nhtsa_safety_ratings as ing
    import autograph.loaders.load_auto_safety as ldr

    # ingestion module
    assert hasattr(ing, "fetch_safety_ratings")
    assert hasattr(ing, "ingest_make_year")
    # loader module
    assert hasattr(ldr, "load_safety")
    assert hasattr(ldr, "LoadStats")
