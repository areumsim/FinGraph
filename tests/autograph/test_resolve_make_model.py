"""`_resolve_make_model_variant` 정합성 단위 테스트.

vPIC brand 중복 시나리오:
  - FORD 가 mfr_id 460/1237/5697 등 다수 entity 로 분할.
  - mfr_id 460 만 실제 FORD 차종 (F-150, Bronco 등) 보유.
  - 옛 LEFT JOIN + LIMIT 1 은 첫 mfr 만 선택 → model_id NULL 적재 버그.
  - 본 fix 후: model_name 매칭 가능한 mfr 우선.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from autograph.loaders.load_auto_pg import _resolve_make_model_variant


def _cursor(fetchone_results):
    """fetchone 호출 순서대로 results 반환."""
    cur = MagicMock()
    cur.fetchone.side_effect = list(fetchone_results)
    return cur


def test_returns_none_for_empty_make():
    cur = MagicMock()
    assert _resolve_make_model_variant(cur, "", "F-150", 2024) == (None, None, None)
    cur.execute.assert_not_called()


def test_make_model_year_all_matched():
    """1단계 (make+model) 조회에서 variant 까지 매칭."""
    cur = _cursor([(460, 7012, 99)])
    result = _resolve_make_model_variant(cur, "FORD", "F-150", 2024)
    assert result == (460, 7012, 99)
    # 1단계만 실행.
    assert cur.execute.call_count == 1


def test_make_model_matched_variant_missing():
    """model 은 매칭, variant 만 없음 — model_id 반환, variant_id=None."""
    cur = _cursor([(460, 7012, None)])
    assert _resolve_make_model_variant(cur, "FORD", "F-150", 2024) == (460, 7012, None)


def test_make_only_fallback_when_no_model_match():
    """model_name 이 어떤 mfr 의 model_name 과도 매칭 안 되면 brand 만 반환."""
    cur = _cursor([None, (460,)])  # 1단계 fail, 2단계 (brand only) 성공.
    assert _resolve_make_model_variant(cur, "FORD", "NONEXISTENT", 2024) == (460, None, None)
    assert cur.execute.call_count == 2


def test_make_unknown_returns_none():
    """make 자체가 PG 에 없으면 모두 None."""
    cur = _cursor([None, None])
    assert _resolve_make_model_variant(cur, "ALIEN_MAKE", "X", 2024) == (None, None, None)


def test_make_only_no_model_name_skips_step1():
    """model_name 이 None/빈문자열이면 1단계 skip, brand 만 조회."""
    cur = _cursor([(460,)])
    assert _resolve_make_model_variant(cur, "FORD", "", 2024) == (460, None, None)
    assert cur.execute.call_count == 1   # brand 만 조회.


def test_brand_duplicate_picks_model_matching_mfr():
    """vPIC brand 중복 (FORD 다수 mfr) — model_name 매칭하는 mfr 우선."""
    # 1단계 cypher 가 INNER JOIN 으로 model_name 매칭하는 mfr 만 후보.
    # 그 중 첫 (smallest model_id) 선택. 본 케이스: mfr=460 의 model 7012 'F-150'.
    cur = _cursor([(460, 7012, 99)])
    result = _resolve_make_model_variant(cur, "FORD", "F-150", 2024)
    # mfr_id=460 (실제 FORD), model_id=7012 ('F-150') 매칭 — 옛 버그 시 mfr=1237 fallback 으로 model_id NULL 가능했음.
    assert result == (460, 7012, 99)
