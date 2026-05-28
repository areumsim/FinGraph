"""temporal_normalizer 단위 테스트 — date stub 으로 시점 고정."""

from __future__ import annotations

from datetime import date

import pytest

from autonexusgraph.agents.temporal import normalize_temporal_terms, extract_year_hint


REF = date(2026, 5, 27)   # FINGRAPH/CLAUDE 환경의 currentDate 기준


def test_single_relative_terms():
    r, a = normalize_temporal_terms("작년 매출은?", reference_date=REF)
    assert "2025년" in r
    assert a["year_from"] == 2025 and a["year_to"] == 2025

    r, a = normalize_temporal_terms("재작년 영업이익", reference_date=REF)
    assert "2024년" in r
    assert a["year_from"] == 2024 and a["year_to"] == 2024

    r, a = normalize_temporal_terms("올해 사업개요", reference_date=REF)
    assert "2026년" in r

    r, a = normalize_temporal_terms("내년 전망", reference_date=REF)
    assert "2027년" in r


def test_range_terms():
    r, a = normalize_temporal_terms("최근 3년 매출 추이", reference_date=REF)
    assert "2024년부터 2026년까지" in r
    assert a["year_from"] == 2024 and a["year_to"] == 2026

    r, a = normalize_temporal_terms("지난 5개년 ROE", reference_date=REF)
    assert "2021년부터 2025년까지" in r

    r, a = normalize_temporal_terms("향후 2년 가이던스", reference_date=REF)
    assert "2027년부터 2028년까지" in r


def test_no_match_keeps_original():
    r, a = normalize_temporal_terms("삼성전자 2023년 매출", reference_date=REF)
    assert r == "삼성전자 2023년 매출"
    assert a["applied"] == []


def test_empty_input():
    r, a = normalize_temporal_terms("", reference_date=REF)
    assert r == ""
    assert a["applied"] == []


def test_extract_year_hint_explicit_wins():
    """질문에 명시적 4자리 연도가 있으면 우선."""
    assert extract_year_hint("삼성전자 2023년 매출", reference_date=REF) == 2023


def test_extract_year_hint_relative_resolves():
    """상대 시간 → 정규화 후 year_to."""
    assert extract_year_hint("작년 매출", reference_date=REF) == 2025
    assert extract_year_hint("최근 3년 매출", reference_date=REF) == 2026


def test_range_bounds():
    """N=0 / 50 초과는 무시."""
    r, a = normalize_temporal_terms("최근 0년 매출", reference_date=REF)
    assert a["year_from"] is None   # 무시
    r, a = normalize_temporal_terms("최근 99년 매출", reference_date=REF)
    assert a["year_from"] is None   # 무시
