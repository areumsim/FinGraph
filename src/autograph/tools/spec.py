"""AutoGraph SQL tool — 차종 식별·제원 조회·비교 (사전 정의 함수 풀).

자유 SQL 금지. LLM 은 함수명 + 파라미터만 결정. READ-ONLY.

모든 함수는 dict / list[dict] 반환 → JSON serializable.
정량 수치는 본 함수 결과만 인용 (LLM 생성 금지).
"""

from __future__ import annotations

from ._db import query_dicts, query_one_dict


DEFAULT_LIMIT = 10
HARD_LIMIT = 200


def _cap(n: int | None, default: int = DEFAULT_LIMIT) -> int:
    if n is None or n <= 0:
        return default
    return min(int(n), HARD_LIMIT)


# ── 식별 ────────────────────────────────────────────────────
def lookup_vehicle(query: str, *,
                   year: int | None = None,
                   limit: int = 5) -> list[dict]:
    """차종 식별 — manufacturer + model + variant (year 옵션).

    매칭 우선순위: model.name 정확 > prefix > substr. year 가 있으면 variant 까지 필터.
    """
    q = (query or "").strip()
    if not q:
        return []
    lim = _cap(limit, 5)
    return query_dicts("""
        SELECT v.variant_id, m.model_id, m.name AS model_name,
               mm.manufacturer_id, mm.name AS mfr_name,
               v.model_year, v.trim, v.fuel_type, v.body_class,
               CASE WHEN m.name ILIKE %(q)s THEN 100
                    WHEN m.name ILIKE %(q)s || '%%' THEN 80
                    WHEN m.name ILIKE '%%' || %(q)s || '%%' THEN 60
                    WHEN mm.name ILIKE '%%' || %(q)s || '%%' THEN 40
                    ELSE 0 END AS score
          FROM auto.master_vehicle_variants v
          JOIN auto.master_vehicle_models m ON v.model_id = m.model_id
          JOIN auto.master_manufacturers mm ON m.manufacturer_id = mm.manufacturer_id
         WHERE (m.name ILIKE '%%' || %(q)s || '%%'
                OR mm.name ILIKE '%%' || %(q)s || '%%')
           AND (%(year)s::int IS NULL OR v.model_year = %(year)s::int)
         ORDER BY score DESC, v.model_year DESC, m.name
         LIMIT %(lim)s
    """, {"q": q, "year": year, "lim": lim})


def get_vehicle_info(variant_id: int) -> dict | None:
    return query_one_dict("""
        SELECT v.variant_id, v.model_year, v.trim, v.fuel_type, v.body_class,
               v.drive_type, v.transmission,
               m.model_id, m.name AS model_name, m.market, m.wikidata_qid,
               mm.manufacturer_id, mm.name AS mfr_name,
               mm.country, mm.wikidata_qid AS mfr_wikidata_qid
          FROM auto.master_vehicle_variants v
          JOIN auto.master_vehicle_models m ON v.model_id = m.model_id
          JOIN auto.master_manufacturers mm ON m.manufacturer_id = mm.manufacturer_id
         WHERE v.variant_id = %s
    """, (variant_id,))


# ── 제원 ────────────────────────────────────────────────────
def get_spec(variant_id: int, measure_key: str | None = None) -> list[dict]:
    """차량 제원 측정값. measure_key 생략 시 모든 키.

    리턴: [{"measure_key", "value_num", "value_text", "unit", "source",
            "confidence", "validated_status", "snapshot_year"}]
    """
    if measure_key:
        return query_dicts("""
            SELECT measure_key, value_num, value_text, unit, source,
                   confidence, validated_status, snapshot_year
              FROM auto.spec_measurements
             WHERE variant_id = %s AND measure_key = %s
             ORDER BY confidence DESC, snapshot_year DESC NULLS LAST
        """, (variant_id, measure_key))
    return query_dicts("""
        SELECT measure_key, value_num, value_text, unit, source,
               confidence, validated_status, snapshot_year
          FROM auto.spec_measurements
         WHERE variant_id = %s
         ORDER BY measure_key, confidence DESC
    """, (variant_id,))


def compare_vehicles(variant_ids: list[int],
                     measure_keys: list[str]) -> list[dict]:
    """여러 차량 × 여러 measure_key 비교. 각 (variant_id, measure_key) 마다 best confidence 값."""
    if not variant_ids or not measure_keys:
        return []
    variant_ids = [int(v) for v in variant_ids][:20]
    measure_keys = [str(k) for k in measure_keys][:20]
    return query_dicts("""
        SELECT DISTINCT ON (variant_id, measure_key)
               variant_id, measure_key, value_num, value_text, unit,
               source, confidence
          FROM auto.spec_measurements
         WHERE variant_id = ANY(%s) AND measure_key = ANY(%s)
         ORDER BY variant_id, measure_key, confidence DESC, snapshot_year DESC NULLS LAST
    """, (variant_ids, measure_keys))


# ── 안전 등급 ────────────────────────────────────────────────
def get_safety_rating(variant_id: int) -> dict | None:
    """NCAP / IIHS 안전 등급.

    ``auto.spec_measurements`` 의 'safety.*' 키를 모두 반환. NHTSA NCAP 은
    `load_auto_safety` 가 'safety.ncap.*' / 'safety.feature.*' 로 채운다.
    KNCAP / Euro NCAP / IIHS 는 별도 ingest 모듈이 추가되면 같은 prefix 로 합류.
    """
    rows = query_dicts("""
        SELECT measure_key, value_num, value_text, unit, source, confidence
          FROM auto.spec_measurements
         WHERE variant_id = %s AND measure_key LIKE 'safety.%%'
         ORDER BY measure_key
    """, (variant_id,))
    if not rows:
        return None
    return {"variant_id": variant_id, "ratings": rows}


__all__ = [
    "lookup_vehicle",
    "get_vehicle_info",
    "get_spec",
    "compare_vehicles",
    "get_safety_rating",
]
