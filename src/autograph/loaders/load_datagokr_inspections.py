"""data.go.kr 15155857 — KOTSA 수리검사내역 → auto.events_inspections UPSERT.

raw 파일 위치: ``data/raw/auto/datagokr_inspections/<year>.jsonl``
             (ingestion.datagokr_inspections 가 CSV → JSONL normalize 한 후 생성)

raw 파일 없으면 graceful skip — exit 0.

CLI:
    python -m autograph.loaders.load_datagokr_inspections
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name


log = logging.getLogger(__name__)


_SOURCE_PATH = "auto/datagokr_inspections"
_SOURCE_TAG = "datagokr_kotsa"


def _resolve_manufacturer_id(cur, raw_name: str | None) -> int | None:
    if not raw_name:
        return None
    norm = normalize_corp_name(raw_name)
    cur.execute("""
        SELECT manufacturer_id FROM auto.master_manufacturers
         WHERE name_norm = %s OR name = %s
         ORDER BY manufacturer_id LIMIT 1
    """, (norm, raw_name))
    r = cur.fetchone()
    return r[0] if r else None


def run() -> dict:
    raw_root = get_settings().ingest_raw_dir / _SOURCE_PATH
    if not raw_root.exists():
        log.warning("[load:inspections] %s 없음 — graceful skip", raw_root)
        return {"inserted": 0, "updated": 0, "skipped": 0}

    files = sorted(raw_root.glob("*.jsonl"))
    if not files:
        log.warning("[load:inspections] %s 에 jsonl 없음 — graceful skip", raw_root)
        return {"inserted": 0, "updated": 0, "skipped": 0}

    conn = get_connection()
    inserted = updated = skipped = 0

    with conn.cursor() as cur:
        for jsonl in files:
            year = jsonl.stem
            try:
                snapshot_year = int(year)
            except ValueError:
                snapshot_year = None
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                cur.execute("SAVEPOINT sp_dg_insp")
                try:
                    mfr_id = _resolve_manufacturer_id(cur, row.get("make_kr"))
                    cur.execute("""
                        INSERT INTO auto.events_inspections
                          (source, source_inspection_id, vin,
                           manufacturer_id, model_id, variant_id,
                           inspection_type, result, inspected_at, reason,
                           snapshot_year, raw)
                        VALUES (%s, %s, %s,
                                %s, NULL, NULL,
                                %s, %s,
                                NULLIF(%s, '')::date, %s,
                                COALESCE(%s, EXTRACT(YEAR FROM now())::SMALLINT),
                                %s::jsonb)
                        ON CONFLICT (source, source_inspection_id) DO UPDATE SET
                          raw = EXCLUDED.raw,
                          ingested_at = now()
                        RETURNING (xmax = 0) AS is_new
                    """, (
                        _SOURCE_TAG,
                        str(row.get("inspection_id") or ""),
                        row.get("vin"),
                        mfr_id,
                        row.get("inspection_type"),
                        row.get("result"),
                        row.get("inspected_at") or "",
                        row.get("reason"),
                        snapshot_year,
                        json.dumps(row, ensure_ascii=False),
                    ))
                    is_new = cur.fetchone()[0]
                    cur.execute("RELEASE SAVEPOINT sp_dg_insp")
                    if is_new:
                        inserted += 1
                    else:
                        updated += 1
                except Exception as exc:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT sp_dg_insp")
                    log.warning("[load:inspections] %s/%s 실패: %s",
                                jsonl.name, row.get("inspection_id"), exc)
                    skipped += 1
    conn.commit()
    log.info("[load:inspections] inserted=%d updated=%d skipped=%d",
             inserted, updated, skipped)
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run"]
