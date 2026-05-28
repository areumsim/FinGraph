"""data/raw/auto/epa_fueleconomy/vehicles.csv(.zip) → auto.spec_measurements.

PRD §3.5: EPA 인증 = A 등급 (0.95). 정량 수치 → number_guard 통과 후 cite 가능.
"제원 수치 EM 95%+" (PRD §10.9) 직접 기여.

매칭 흐름:
    1) CSV row 의 (year, make, model) → name_norm 기반 variant_id 매칭.
    2) 매칭된 모든 variant 에 동일 값 박음 (EPA 가 trim 단위라 1:N 가능).
    3) source='epa_fueleconomy' 기존 행은 DELETE 후 재INSERT (멱등).

매핑 (CSV 컬럼 → measure_key, unit, value_type):
    city08              → spec.efficiency.mpg_city                 (mpg, num)
    highway08           → spec.efficiency.mpg_highway              (mpg, num)
    comb08              → spec.efficiency.mpg_combined             (mpg, num)
    city08U             → spec.efficiency.mpg_city_unrounded       (mpg, num)
    highway08U          → spec.efficiency.mpg_highway_unrounded    (mpg, num)
    comb08U             → spec.efficiency.mpg_combined_unrounded   (mpg, num)
    cylinders           → spec.engine.cylinders                    (count, num)
    displ               → spec.engine.displacement_l               (L, num)
    fuelType            → spec.engine.fuel_type                    (text)
    fuelType1           → spec.engine.fuel_type_primary            (text)
    atvtype             → spec.engine.alt_tech_type                (text)
    evMotor             → spec.engine.ev_motor_kw                  (kW, num)
    trany               → spec.transmission.type                   (text)
    drive               → spec.drivetrain.type                     (text)
    co2                 → spec.emissions.co2_g_per_mile            (g/mile, num)
    ghgScore            → spec.emissions.ghg_score                 (score, num — 1~10)
    feScore             → spec.efficiency.epa_fe_score             (score, num — 1~10)
    fuelCost08          → spec.cost.annual_fuel_usd                (USD, num)
    sCharger            → spec.engine.supercharger                 (text Y/N)
    tCharger            → spec.engine.turbocharger                 (text Y/N)
    startStop           → spec.feature.start_stop                  (text Y/N)
    charge240           → spec.battery.charge_240v_hours           (hours, num)
    combE               → spec.efficiency.kwh_per_100mi_combined   (kWh/100mi, num)
    guzzler             → spec.regulatory.gas_guzzler              (text)
    VClass              → spec.body.epa_size_class                 (text)

Sentinel '-1' / 빈 문자열 / 'N' 단독은 측정값 없음으로 skip.

CLI:
    python -m autograph.loaders.load_auto_epa
    python -m autograph.loaders.load_auto_epa --year-min 2020
    python -m autograph.loaders.load_auto_epa --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name


log = logging.getLogger(__name__)


_SOURCE_KEY = "epa_fueleconomy"
_CONFIDENCE = 0.95   # PRD §3.5 A 등급


# (csv_field, measure_key, unit, value_type)
#   value_type: 'num' (float, sentinel -1 = missing),
#               'score' (1~10 정수, -1=missing),
#               'text' (단순 텍스트),
#               'yn'   ('Y' 만 True 로, 빈/N 은 skip),
#               'count' (정수)
_MAP: list[tuple[str, str, str, str]] = [
    ("city08",      "spec.efficiency.mpg_city",                "mpg", "num"),
    ("highway08",   "spec.efficiency.mpg_highway",             "mpg", "num"),
    ("comb08",      "spec.efficiency.mpg_combined",            "mpg", "num"),
    ("city08U",     "spec.efficiency.mpg_city_unrounded",      "mpg", "num"),
    ("highway08U",  "spec.efficiency.mpg_highway_unrounded",   "mpg", "num"),
    ("comb08U",     "spec.efficiency.mpg_combined_unrounded",  "mpg", "num"),
    ("cylinders",   "spec.engine.cylinders",                   "",    "count"),
    ("displ",       "spec.engine.displacement_l",              "L",   "num"),
    ("fuelType",    "spec.engine.fuel_type",                   "",    "text"),
    ("fuelType1",   "spec.engine.fuel_type_primary",           "",    "text"),
    ("atvtype",     "spec.engine.alt_tech_type",               "",    "text"),
    ("evMotor",     "spec.engine.ev_motor_kw",                 "kW",  "num"),
    ("trany",       "spec.transmission.type",                  "",    "text"),
    ("drive",       "spec.drivetrain.type",                    "",    "text"),
    ("co2",         "spec.emissions.co2_g_per_mile",           "g/mile", "num"),
    ("ghgScore",    "spec.emissions.ghg_score",                "score", "score"),
    ("feScore",     "spec.efficiency.epa_fe_score",            "score", "score"),
    ("fuelCost08",  "spec.cost.annual_fuel_usd",               "USD", "num"),
    ("sCharger",    "spec.engine.supercharger",                "",    "yn"),
    ("tCharger",    "spec.engine.turbocharger",                "",    "yn"),
    ("startStop",   "spec.feature.start_stop",                 "",    "yn"),
    ("charge240",   "spec.battery.charge_240v_hours",          "hours", "num"),
    ("combE",       "spec.efficiency.kwh_per_100mi_combined",  "kWh/100mi", "num"),
    ("guzzler",     "spec.regulatory.gas_guzzler",             "",    "text"),
    ("VClass",      "spec.body.epa_size_class",                "",    "text"),
]


@dataclass
class LoadStats:
    rows_seen:           int = 0
    rows_year_filtered:  int = 0
    rows_unmatched:      int = 0
    rows_matched:        int = 0
    variants_touched:    int = 0
    measurements_inserted: int = 0
    measurements_replaced: int = 0
    errors: list[str] = field(default_factory=list)


def _epa_root() -> Path:
    return get_settings().ingest_raw_dir / "auto" / "epa_fueleconomy"


def _iter_csv_rows(path: Path) -> Iterator[dict]:
    """vehicles.csv.zip 또는 vehicles.csv 모두 지원. 헤더 유추 → dict yield."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            # zip 안 csv 파일 (보통 vehicles.csv 단일).
            csv_names = [n for n in z.namelist() if n.endswith(".csv")]
            if not csv_names:
                raise FileNotFoundError(f"zip 안 csv 없음: {path}")
            with z.open(csv_names[0]) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
                for row in reader:
                    yield row
    else:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield row


def _parse_value(raw: str | None, vtype: str) -> tuple[float | None, str | None]:
    """raw → (value_num, value_text). vtype 별 sentinel/empty 처리.

    Returns:
        (value_num, value_text) — 둘 중 하나는 None.
        둘 다 None 이면 row skip 신호.
    """
    if raw is None:
        return None, None
    s = str(raw).strip()
    if not s:
        return None, None

    if vtype == "yn":
        # 'Y' 또는 명시적 비공백만 인정. 'N' / 빈문자는 skip.
        if s.upper() in ("Y", "YES", "TRUE", "1", "S", "T"):
            return None, "Y"
        return None, None

    if vtype == "text":
        # EPA text 필드는 가끔 'N/A', '-' 등 — 의미 있는 텍스트만.
        if s.upper() in ("N/A", "-", "NONE"):
            return None, None
        return None, s[:400]

    # 숫자 류 (num / score / count).
    try:
        v = float(s)
    except ValueError:
        return None, None

    if vtype == "score":
        # -1 = "Not available" sentinel.
        if v < 0 or v > 10:
            return None, None
        return v, None
    if vtype == "count":
        if v < 0:
            return None, None
        return v, None
    if vtype == "num":
        # -1 sentinel + 음수 (cost 외엔) 일반적으로 missing.
        if v == -1.0:
            return None, None
        return v, None

    return None, None


def _resolve_variants(cur, *, make: str, model: str, year: int) -> list[int]:
    """(make, model, year) → 매칭되는 variant_id 목록 (NCAP loader 와 동일 패턴).

    name_norm 정확 매칭 우선, 같은 prefix 까지 허용.
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
        int(year),
        normalize_corp_name(model),
    ))
    return [r[0] for r in cur.fetchall()]


def _process_row(cur, row: dict, stats: LoadStats,
                 *, year_min: int | None) -> None:
    stats.rows_seen += 1
    year_raw = row.get("year")
    make = row.get("make") or ""
    model = row.get("model") or ""
    try:
        year = int(year_raw) if year_raw else 0
    except (TypeError, ValueError):
        return
    if year <= 0:
        return
    if year_min is not None and year < year_min:
        stats.rows_year_filtered += 1
        return

    variant_ids = _resolve_variants(cur, make=make, model=model, year=year)
    if not variant_ids:
        stats.rows_unmatched += 1
        return
    stats.rows_matched += 1

    # 측정값 추출 — sentinel 제거된 (key, value_num, value_text, unit) 목록.
    measurements: list[tuple[str, float | None, str | None, str]] = []
    for csv_field, measure_key, unit, vtype in _MAP:
        v_num, v_text = _parse_value(row.get(csv_field), vtype)
        if v_num is None and v_text is None:
            continue
        measurements.append((measure_key, v_num, v_text, unit))

    if not measurements:
        return

    raw_compact = {
        "id": row.get("id"),
        "year": year,
        "make": make,
        "model": model,
        "VClass": row.get("VClass"),
    }
    raw_json = json.dumps(raw_compact, ensure_ascii=False, default=str)
    source_ref = f"{make}|{model}|{year}|epaid={row.get('id', '')}"

    for vid in variant_ids:
        cur.execute("SAVEPOINT sp_epa")
        try:
            # 같은 source 기존 row 모두 삭제 (멱등).
            cur.execute("""
                DELETE FROM auto.spec_measurements
                 WHERE variant_id = %s AND source = %s
            """, (vid, _SOURCE_KEY))
            deleted = cur.rowcount

            inserted = 0
            for measure_key, v_num, v_text, unit in measurements:
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
                    _SOURCE_KEY, source_ref, _CONFIDENCE,
                    year, raw_json,
                ))
                inserted += 1
            cur.execute("RELEASE SAVEPOINT sp_epa")
            stats.variants_touched += 1
            stats.measurements_inserted += inserted
            if deleted:
                stats.measurements_replaced += min(deleted, inserted)
        except Exception as e:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT sp_epa")
            stats.errors.append(f"variant {vid} {make}/{model}/{year}: {e}")


def load_epa(*, dry_run: bool = False,
             year_min: int | None = None) -> LoadStats:
    stats = LoadStats()
    root = _epa_root()
    # zip 우선, 없으면 압축해제된 csv.
    candidates = [root / "vehicles.csv.zip", root / "vehicles.csv"]
    src = next((p for p in candidates if p.exists()), None)
    if src is None:
        log.warning("[load:epa] vehicles.csv(.zip) 없음 — ingestion 먼저 실행: %s",
                    root)
        return stats

    conn = get_connection()
    with conn.cursor() as cur:
        for row in _iter_csv_rows(src):
            _process_row(cur, row, stats, year_min=year_min)

    if dry_run:
        conn.rollback()
        log.info(
            "[load:epa] DRY-RUN seen=%d filtered=%d unmatched=%d matched=%d "
            "variants=%d measurements=%d",
            stats.rows_seen, stats.rows_year_filtered, stats.rows_unmatched,
            stats.rows_matched, stats.variants_touched,
            stats.measurements_inserted,
        )
        return stats

    conn.commit()
    log.info(
        "[load:epa] seen=%d filtered=%d unmatched=%d matched=%d "
        "variants=%d measurements=%d (replaced=%d) errors=%d",
        stats.rows_seen, stats.rows_year_filtered, stats.rows_unmatched,
        stats.rows_matched, stats.variants_touched,
        stats.measurements_inserted, stats.measurements_replaced,
        len(stats.errors),
    )
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_epa")
    ap.add_argument("--year-min", type=int, default=None,
                    help="이 연식 미만 row skip (기본 전체)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_epa(dry_run=args.dry_run, year_min=args.year_min)


if __name__ == "__main__":
    main()


__all__ = [
    "load_epa", "LoadStats",
    "_MAP", "_parse_value", "_resolve_variants",
    "_SOURCE_KEY", "_CONFIDENCE",
]
