"""data/raw/auto/nhtsa_safety/**/*.json → auto.spec_measurements + Neo4j SAFETY_RATED_BY.

PRD §3.5: NCAP 출처는 A 등급 (confidence 0.95).
- `auto.spec_measurements` 의 ``safety.ncap.*`` measure_key 채움 → ``get_safety_rating()`` 가
  더 이상 None 만 반환 안 함.
- Neo4j ``(VehicleVariant)-[:SAFETY_RATED_BY {confidence_score:0.95, ...}]->(Standard {code:'NCAP_US'})``
  엣지 적재 → ``auto_safety_ratings`` cypher 가 빈 결과만 주던 문제 해소.

매핑 (response field → measure_key):
    OverallRating                       → safety.ncap.overall_5star          (1~5)
    OverallFrontCrashRating             → safety.ncap.frontal_overall
    FrontCrashDriversideRating          → safety.ncap.frontal_driver
    FrontCrashPassengersideRating       → safety.ncap.frontal_passenger
    OverallSideCrashRating              → safety.ncap.side_overall
    SideCrashDriversideRating           → safety.ncap.side_driver
    SideCrashPassengersideRating        → safety.ncap.side_passenger
    SidePoleCrashRating                 → safety.ncap.side_pole
    RolloverRating                      → safety.ncap.rollover
    RolloverPossibility ("12.3%")       → safety.ncap.rollover_pct (%, value_num)
    NHTSAElectronicStabilityControl     → safety.feature.esc       (value_text)
    NHTSAForwardCollisionWarning        → safety.feature.fcw
    NHTSALaneDepartureWarning           → safety.feature.ldw

Bug 회피: NHTSA SafetyRatings 의 trim 표기 ("AWD ELECTRIC", "STANDARD RANGE", ...) 는
`auto.master_vehicle_variants.trim` 과 정확 매칭이 어렵다. 본 loader 는 (make, model, year)
까지만 매칭하고 매칭된 모든 variant 에 동일 점수를 박는다 — NCAP 점수는 보통 trim
범위에 걸쳐 동일이라 안전. 단, 동일 model_year × trim 다수가 있으면 적당히 보수적.

CLI:
    python -m autograph.loaders.load_auto_safety
    python -m autograph.loaders.load_auto_safety --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from autonexusgraph.config import get_settings
from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name

from ._neo4j_helpers import run_batched


log = logging.getLogger(__name__)


# response 키 → (measure_key, unit, value_type)
# value_type: 'star' (1~5 정수), 'pct' (% 숫자), 'text' (Standard/Optional/...)
_RATING_MAP: dict[str, tuple[str, str, str]] = {
    "OverallRating":                  ("safety.ncap.overall_5star",   "star", "star"),
    "OverallFrontCrashRating":        ("safety.ncap.frontal_overall", "star", "star"),
    "FrontCrashDriversideRating":     ("safety.ncap.frontal_driver",  "star", "star"),
    "FrontCrashPassengersideRating":  ("safety.ncap.frontal_passenger","star","star"),
    "OverallSideCrashRating":         ("safety.ncap.side_overall",    "star", "star"),
    "SideCrashDriversideRating":      ("safety.ncap.side_driver",     "star", "star"),
    "SideCrashPassengersideRating":   ("safety.ncap.side_passenger",  "star", "star"),
    "SidePoleCrashRating":            ("safety.ncap.side_pole",       "star", "star"),
    "RolloverRating":                 ("safety.ncap.rollover",        "star", "star"),
    "RolloverPossibility":            ("safety.ncap.rollover_pct",    "percent", "pct"),
    "NHTSAElectronicStabilityControl":("safety.feature.esc",          "",     "text"),
    "NHTSAForwardCollisionWarning":   ("safety.feature.fcw",          "",     "text"),
    "NHTSALaneDepartureWarning":      ("safety.feature.ldw",          "",     "text"),
}


_STANDARD_CODE = "NCAP_US"
_NCAP_CONFIDENCE = 0.95   # PRD §3.5 A 등급
_SOURCE_KEY = "nhtsa_safety_ratings"


@dataclass
class LoadStats:
    files_seen:     int = 0
    files_empty:    int = 0
    rows_inserted:  int = 0
    rows_replaced:  int = 0
    edges_written:  int = 0
    variants_unmatched: int = 0
    errors: list[str] = field(default_factory=list)


def _auto_safety_root() -> Path:
    return get_settings().ingest_raw_dir / "auto" / "nhtsa_safety"


def _parse_star(value: str | None) -> float | None:
    """'5' / '4' / 'Not Rated' / '' → float | None."""
    if not value:
        return None
    s = str(value).strip()
    if not s or not s[0].isdigit():
        return None
    try:
        # 1~5 정수 기대. 가끔 '4.5' 도 있음.
        v = float(s)
        if v < 0 or v > 5:
            return None
        return v
    except ValueError:
        return None


def _parse_pct(value: str | None) -> float | None:
    """'12.34%' → 12.34. None / 비숫자 → None."""
    if not value:
        return None
    s = str(value).strip().rstrip("%").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _resolve_variants(cur, *, make: str, model: str, year: int) -> list[int]:
    """(make, model, year) → 매칭되는 variant_id 목록 (1개 이상).

    NCAP 점수는 보통 trim 전체에 동일이라 매칭된 모든 variant 에 박는다.
    name_norm 정확 + prefix 매칭 시도.
    """
    if not (make and model and year):
        return []
    cur.execute("""
        SELECT v.variant_id
          FROM auto.master_vehicle_variants v
          JOIN auto.master_vehicle_models m  USING (model_id)
          JOIN auto.master_manufacturers mm USING (manufacturer_id)
         WHERE mm.name_norm = %s
           AND (m.name_norm = %s OR m.name_norm LIKE %s)
           AND v.model_year = %s
         ORDER BY m.name_norm = %s DESC, length(m.name_norm) ASC
    """, (
        normalize_corp_name(make),
        normalize_corp_name(model),
        normalize_corp_name(model) + "%",
        year,
        normalize_corp_name(model),
    ))
    return [r[0] for r in cur.fetchall()]


# Neo4j (VehicleVariant)-[:SAFETY_RATED_BY]->(Standard {code:'NCAP_US'})
_MERGE_SAFETY_RATED_BY = """
UNWIND $rows AS r
MATCH (v:VehicleVariant {id: r.variant_id})
MATCH (s:Standard {code: r.standard_code})
MERGE (v)-[rel:SAFETY_RATED_BY]->(s)
SET   rel.source_type      = 'pg.auto.spec_measurements/nhtsa',
      rel.source_id        = r.source_id,
      rel.extraction_method= 'deterministic',
      rel.confidence_score = r.confidence,
      rel.validated_status = 'verified',
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year),
      rel.overall_rating   = r.overall_rating
"""


def load_safety(*, dry_run: bool = False, batch: int = 200) -> LoadStats:
    stats = LoadStats()
    root = _auto_safety_root()
    if not root.exists():
        log.warning("[load:safety] root missing: %s", root)
        return stats

    conn = get_connection()
    edges: list[dict] = []

    with conn.cursor() as cur:
        for f in root.glob("*/*/*.json"):
            stats.files_seen += 1
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                stats.errors.append(f"safety json {f}: {e}")
                continue

            results = data.get("Results") or []
            if not results:
                stats.files_empty += 1
                continue

            # path = .../nhtsa_safety/{MAKE}/{MODEL}/{YEAR}.json
            try:
                make_dir  = f.parent.parent.name
                model_dir = f.parent.name
                year_dir  = int(f.stem)
            except (ValueError, IndexError):
                stats.errors.append(f"safety path parse {f}")
                continue

            variant_ids = _resolve_variants(
                cur, make=make_dir, model=model_dir, year=year_dir,
            )
            if not variant_ids:
                stats.variants_unmatched += 1
                continue

            # SafetyRatings 응답은 trim 별 row 가 여러 개 — 첫 row 의 점수를 사용 (NCAP 동일).
            # 대표 row 선정: OverallRating 이 있는 첫 항목.
            primary = next(
                (r for r in results if _parse_star(r.get("OverallRating")) is not None),
                results[0],
            )
            overall = _parse_star(primary.get("OverallRating"))

            for vid in variant_ids:
                cur.execute("SAVEPOINT sp_safety")
                try:
                    # 동일 source 기존 측정값 모두 삭제 후 재삽입 (멱등).
                    cur.execute("""
                        DELETE FROM auto.spec_measurements
                         WHERE variant_id = %s AND source = %s
                    """, (vid, _SOURCE_KEY))
                    deleted = cur.rowcount

                    inserted_this_variant = 0
                    for field_key, (measure_key, unit, vtype) in _RATING_MAP.items():
                        raw_val = primary.get(field_key)
                        if raw_val in (None, ""):
                            continue

                        if vtype == "star":
                            v_num = _parse_star(raw_val)
                            if v_num is None:
                                continue
                            v_text = None
                        elif vtype == "pct":
                            v_num = _parse_pct(raw_val)
                            if v_num is None:
                                continue
                            v_text = None
                        else:   # text
                            v_num = None
                            v_text = str(raw_val)[:400]

                        cur.execute("""
                            INSERT INTO auto.spec_measurements
                              (variant_id, measure_key, value_num, value_text, unit,
                               source, source_ref, confidence, validated_status,
                               snapshot_year, raw)
                            VALUES (%s, %s, %s, %s, %s,
                                    %s, %s, %s, 'verified',
                                    %s, %s::jsonb)
                        """, (
                            vid, measure_key, v_num, v_text, unit or None,
                            _SOURCE_KEY,
                            f"{make_dir}|{model_dir}|{year_dir}|"
                            f"{primary.get('VehicleId','')}",
                            _NCAP_CONFIDENCE,
                            year_dir,
                            json.dumps({"raw": raw_val, "field": field_key,
                                        "VehicleId": primary.get("VehicleId"),
                                        "VehicleDescription": primary.get("VehicleDescription")},
                                       ensure_ascii=False),
                        ))
                        inserted_this_variant += 1

                    cur.execute("RELEASE SAVEPOINT sp_safety")
                    stats.rows_inserted += inserted_this_variant
                    if deleted:
                        stats.rows_replaced += min(deleted, inserted_this_variant)

                    # Neo4j edge 후보 (NCAP_US standard 노드 향).
                    if inserted_this_variant > 0:
                        edges.append({
                            "variant_id": int(vid),
                            "standard_code": _STANDARD_CODE,
                            "source_id": f"nhtsa_safety:{primary.get('VehicleId','')}",
                            "confidence": _NCAP_CONFIDENCE,
                            "snapshot_year": year_dir,
                            "overall_rating": overall,
                        })
                except Exception as e:   # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT sp_safety")
                    stats.errors.append(f"safety row {f} variant={vid}: {e}")

    if dry_run:
        conn.rollback()
        log.info("[load:safety] DRY-RUN rolled back. would insert=%d edges=%d",
                 stats.rows_inserted, len(edges))
        return stats

    conn.commit()

    # Neo4j 적재. Standard 노드는 load_seed_standards_plants 가 먼저 만들어둠.
    if edges:
        driver = get_driver()
        with driver.session() as session:
            stats.edges_written = run_batched(
                session, _MERGE_SAFETY_RATED_BY, edges, batch=batch,
            )

    log.info(
        "[load:safety] files=%d (empty=%d) inserted=%d replaced=%d "
        "edges=%d unmatched=%d errors=%d",
        stats.files_seen, stats.files_empty,
        stats.rows_inserted, stats.rows_replaced,
        stats.edges_written, stats.variants_unmatched, len(stats.errors),
    )
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_safety")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_safety(dry_run=args.dry_run, batch=args.batch)


if __name__ == "__main__":
    main()


__all__ = ["load_safety", "LoadStats", "_RATING_MAP", "_parse_star", "_parse_pct"]
