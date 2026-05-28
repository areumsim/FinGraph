"""vPIC Canadian Vehicle Specifications → auto.spec_measurements 적재.

raw 위치:
    data/raw/auto/nhtsa_vpic/{MAKE}/{YEAR}/canspec_ALL.json

각 ``Results[*].Specs`` 리스트의 (Name, Value) 쌍을 표준화된 measure_key 로 매핑.
NHTSA Canadian Specs 의 단위는 dim=cm, weight=kg.

매핑 (이름 → measure_key, 단위, scale):
    OL  Overall Length      → spec.dim.length_mm     (cm→mm ×10)
    OW  Overall Width        → spec.dim.width_mm     (cm→mm ×10)
    OH  Overall Height       → spec.dim.height_mm    (cm→mm ×10)
    WB  Wheelbase            → spec.dim.wheelbase_mm (cm→mm ×10)
    CW  Curb Weight          → spec.weight.curb_kg   (kg, identity)
    TWF Track Width Front    → spec.dim.track_front_mm
    TWR Track Width Rear     → spec.dim.track_rear_mm
    MYR Model Year           → (variant 매칭에만 사용, INSERT 안 함)
    Make / Model             → (variant 매칭에만 사용)
    A/B/C/D/E/F/G/WD         → 의미 불명 — raw JSON 에 보관, INSERT 안 함

variant 매칭:
    canspec 의 Make + Model + 20+MYR → auto.master_vehicle_variants 의
    (manufacturer name_norm, model name_norm prefix, model_year) 매칭. 미매칭 시 skip.

UPSERT 규칙:
    UNIQUE (variant_id, measure_key, source) — 같은 source 재실행 시 갱신.
    제약은 schema 에 없으나 본 loader 가 DELETE+INSERT 로 동일 효과.

CLI:
    python -m autograph.loaders.load_auto_specs
    python -m autograph.loaders.load_auto_specs --make HYUNDAI --year 2024
    python -m autograph.loaders.load_auto_specs --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name


log = logging.getLogger(__name__)


# Name → (measure_key, unit, scale_to_unit)
_SPEC_MAP: dict[str, tuple[str, str, float]] = {
    "OL":  ("spec.dim.length_mm",     "mm", 10.0),
    "OW":  ("spec.dim.width_mm",      "mm", 10.0),
    "OH":  ("spec.dim.height_mm",     "mm", 10.0),
    "WB":  ("spec.dim.wheelbase_mm",  "mm", 10.0),
    "CW":  ("spec.weight.curb_kg",    "kg", 1.0),
    "TWF": ("spec.dim.track_front_mm", "mm", 10.0),
    "TWR": ("spec.dim.track_rear_mm",  "mm", 10.0),
}


# canspec Model 문자열 → (body_class, drive_type).
# 예: 'IONIQ 6 4DR SEDAN'              → ('Sedan', None)
#     'PALISADE 4DR SUV AWD'           → ('SUV', 'AWD')
#     'SANTA FE 4DR SUV FWD HEV'       → ('SUV', 'FWD')
#     'KONA EV 4DR SUV AWD'            → ('SUV', 'AWD')
#     'ELANTRA 4DR HATCHBACK FWD'      → ('Hatchback', 'FWD')
_BODY_KEYWORDS = (
    ("SUV", "SUV"),
    ("HATCHBACK", "Hatchback"),
    ("WAGON", "Wagon"),
    ("COUPE", "Coupe"),
    ("CONVERTIBLE", "Convertible"),
    ("PICKUP", "Pickup"),
    ("VAN", "Van"),
    ("SEDAN", "Sedan"),
    ("TRUCK", "Truck"),
)
_DRIVE_KEYWORDS = ("AWD", "4WD", "FWD", "RWD")


def parse_canspec_model_str(model_raw: str) -> tuple[str | None, str | None]:
    """canspec 의 Model 문자열에서 (body_class, drive_type) 추출.

    raw 값은 "<MODEL> [NDR] <BODY> [DRIVE] [POWERTRAIN]" 형태.
    매칭 실패 시 (None, None).
    """
    if not model_raw:
        return None, None
    up = model_raw.upper()
    body = next((bv for kw, bv in _BODY_KEYWORDS if kw in up), None)
    drive = next((kw for kw in _DRIVE_KEYWORDS if kw in up), None)
    return body, drive


@dataclass
class LoadStats:
    files_seen: int = 0
    rows_inserted: int = 0
    rows_replaced: int = 0
    variants_unmatched: int = 0
    errors: list[str] = field(default_factory=list)


def _auto_vpic_root() -> Path:
    return get_settings().ingest_raw_dir / "auto" / "nhtsa_vpic"


def _specs_dict(specs_list: list[dict]) -> dict[str, str]:
    """[{Name, Value}, ...] → {Name: Value}."""
    out: dict[str, str] = {}
    for s in specs_list or []:
        name = (s.get("Name") or "").strip()
        val = (s.get("Value") or "").strip()
        if name:
            out[name] = val
    return out


def _resolve_variant(cur, *, make: str, model_raw: str, model_year: int
                     ) -> int | None:
    """canspec 의 Make + Model('IONIQ 6 4DR SEDAN') + year → variant_id.

    canspec Model 은 "IONIQ 6 4DR SEDAN" 같이 trim/body 가 붙어있어 vPIC 모델명
    ('Ioniq 6') 과 정확 일치 안 함. prefix 토큰 1~2 개 매칭으로 best-effort.
    """
    if not (make and model_raw and model_year):
        return None
    # canspec model 첫 1~2 토큰만 keep — "IONIQ 6 4DR SEDAN" → "IONIQ 6" 또는 "IONIQ"
    tokens = [t for t in model_raw.split() if t and not t[0].isdigit() or len(t) <= 2]
    candidates = [" ".join(model_raw.split()[:2]),
                  " ".join(model_raw.split()[:1])]
    for cand in candidates:
        if not cand:
            continue
        cur.execute("""
            SELECT v.variant_id
              FROM auto.master_vehicle_variants v
              JOIN auto.master_vehicle_models m  USING (model_id)
              JOIN auto.master_manufacturers mm USING (manufacturer_id)
             WHERE mm.name_norm = %s
               AND (m.name_norm = %s OR m.name_norm LIKE %s)
               AND v.model_year = %s
             ORDER BY m.name_norm = %s DESC, length(m.name_norm) ASC
             LIMIT 1
        """, (normalize_corp_name(make),
              normalize_corp_name(cand),
              normalize_corp_name(cand) + "%",
              model_year,
              normalize_corp_name(cand)))
        r = cur.fetchone()
        if r:
            return r[0]
    return None


def load_specs(*, make_filter: str | None = None,
               year_filter: int | None = None,
               dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    root = _auto_vpic_root()
    if not root.exists():
        log.warning("[load:specs] root missing: %s", root)
        return stats

    conn = get_connection()
    with conn.cursor() as cur:
        for canspec_path in root.glob("*/*/canspec_ALL.json"):
            # path = .../nhtsa_vpic/{MAKE}/{YEAR}/canspec_ALL.json
            try:
                make_dir = canspec_path.parent.parent.name
                year_dir = int(canspec_path.parent.name)
            except (ValueError, IndexError):
                continue
            if make_filter and make_dir.upper() != make_filter.upper():
                continue
            if year_filter and year_dir != year_filter:
                continue
            stats.files_seen += 1

            try:
                data = json.loads(canspec_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                stats.errors.append(f"specs json {canspec_path}: {e}")
                continue

            for result in data.get("Results") or []:
                cur.execute("SAVEPOINT sp_specs")
                try:
                    specs = _specs_dict(result.get("Specs") or [])
                    make = specs.get("Make") or make_dir
                    model_raw = specs.get("Model") or ""
                    myr_raw = specs.get("MYR")
                    # MYR 가 '23' 같은 2자리 → 4자리.
                    try:
                        myr = int(myr_raw)
                        if myr < 100:
                            myr += 2000
                    except (TypeError, ValueError):
                        myr = year_dir

                    variant_id = _resolve_variant(
                        cur, make=make, model_raw=model_raw, model_year=myr)
                    if variant_id is None:
                        stats.variants_unmatched += 1
                        cur.execute("RELEASE SAVEPOINT sp_specs")
                        continue

                    # canspec Model 문자열에서 body_class / drive_type 보강 —
                    # vPIC GetModelsForMakeYear 는 trim/body/drive 를 반환하지 않으므로
                    # canspec 의 'IONIQ 6 4DR SEDAN' / 'PALISADE 4DR SUV AWD' 등에서 추출.
                    body_class, drive_type = parse_canspec_model_str(model_raw)
                    if body_class or drive_type:
                        cur.execute("""
                            UPDATE auto.master_vehicle_variants
                               SET body_class = COALESCE(body_class, %s),
                                   drive_type = COALESCE(drive_type, %s)
                             WHERE variant_id = %s
                               AND (body_class IS NULL OR drive_type IS NULL)
                        """, (body_class, drive_type, variant_id))

                    # 기존 동일 source 측정값 모두 삭제 후 재삽입 (멱등).
                    cur.execute("""
                        DELETE FROM auto.spec_measurements
                         WHERE variant_id = %s AND source = 'nhtsa_canspec'
                    """, (variant_id,))
                    deleted = cur.rowcount

                    inserted_this_variant = 0
                    for name, raw_val in specs.items():
                        m = _SPEC_MAP.get(name)
                        if not m:
                            continue
                        measure_key, unit, scale = m
                        try:
                            value_num = float(raw_val) * scale
                        except (TypeError, ValueError):
                            continue
                        cur.execute("""
                            INSERT INTO auto.spec_measurements
                              (variant_id, measure_key, value_num, value_text, unit,
                               source, source_ref, confidence, validated_status,
                               snapshot_year, raw)
                            VALUES (%s, %s, %s, NULL, %s,
                                    'nhtsa_canspec', %s, 1.000, 'verified',
                                    %s, %s::jsonb)
                        """, (variant_id, measure_key, value_num, unit,
                              f"{make}|{model_raw}|{myr}",
                              myr,
                              json.dumps({"Name": name, "Value": raw_val,
                                          "scale": scale, "src": "canspec_ALL"},
                                         ensure_ascii=False)))
                        inserted_this_variant += 1

                    cur.execute("RELEASE SAVEPOINT sp_specs")
                    stats.rows_inserted += inserted_this_variant
                    if deleted:
                        stats.rows_replaced += min(deleted, inserted_this_variant)
                except Exception as e:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT sp_specs")
                    stats.errors.append(f"specs row {canspec_path}: {e}")

    if dry_run:
        conn.rollback()
        log.info("[load:specs] DRY-RUN rolled back. would insert=%d (replaced=%d)",
                 stats.rows_inserted, stats.rows_replaced)
    else:
        conn.commit()
        log.info("[load:specs] files=%d inserted=%d replaced=%d unmatched=%d errors=%d",
                 stats.files_seen, stats.rows_inserted, stats.rows_replaced,
                 stats.variants_unmatched, len(stats.errors))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_specs")
    ap.add_argument("--make", help="필터: 단일 제조사 (예: HYUNDAI)")
    ap.add_argument("--year", type=int, help="필터: 단일 연식")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_specs(make_filter=args.make, year_filter=args.year, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
