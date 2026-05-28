"""data.go.kr 15089863 — 한국 KOTSA 리콜 → auto.events_recalls UPSERT.

raw 파일 위치: ``data/raw/auto/datagokr_recalls/page_*.json``
적재 키:       ``(source='datagokr_kotsa', source_recall_no=<리콜번호>)``

raw 파일 없으면 graceful skip — exit 0.

CLI:
    python -m autograph.loaders.load_datagokr_recalls
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


_SOURCE_PATH = "auto/datagokr_recalls"
_SOURCE_TAG = "datagokr_kotsa"


def _iter_items(root: Path):
    for f in sorted(root.glob("page_*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("[load:datagokr_recalls] %s 파싱 실패: %s", f.name, exc)
            continue
        items = payload.get("data") or payload.get("items") or []
        for item in items:
            yield f.name, item


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
        log.warning("[load:datagokr_recalls] %s 없음 — graceful skip", raw_root)
        return {"inserted": 0, "updated": 0, "skipped": 0}

    conn = get_connection()
    inserted = updated = skipped = 0

    with conn.cursor() as cur:
        for filename, item in _iter_items(raw_root):
            # 정확한 컬럼명은 data.go.kr 명세에 따라 다름 — 대표 키만.
            recall_no = (item.get("리콜번호") or item.get("recallNo")
                         or item.get("recall_no") or item.get("RECALL_NO"))
            manufacturer_name = (item.get("제작자") or item.get("제작사")
                                 or item.get("manufacturer"))
            model_name = item.get("차명") or item.get("model")
            defect = item.get("결함내용") or item.get("defect")
            remedy = item.get("시정조치") or item.get("remedy")
            report_date = item.get("리콜개시일") or item.get("startDate")

            if not recall_no:
                skipped += 1
                continue

            cur.execute("SAVEPOINT sp_dg_recall")
            try:
                mfr_id = _resolve_manufacturer_id(cur, manufacturer_name)
                cur.execute("""
                    INSERT INTO auto.events_recalls
                      (source, source_recall_no, manufacturer_id, model_id, variant_id,
                       component_text, defect_summary, consequence, remedy_summary,
                       report_date, country, affected_units, raw, snapshot_year)
                    VALUES (%s, %s, %s, NULL, NULL,
                            NULL, %s, NULL, %s,
                            NULLIF(%s, '')::date, %s,
                            NULL,
                            %s::jsonb,
                            COALESCE(
                              EXTRACT(YEAR FROM NULLIF(%s,'')::date)::SMALLINT,
                              EXTRACT(YEAR FROM now())::SMALLINT))
                    ON CONFLICT (source, source_recall_no) DO UPDATE SET
                      manufacturer_id = COALESCE(EXCLUDED.manufacturer_id,
                                                  auto.events_recalls.manufacturer_id),
                      raw             = EXCLUDED.raw,
                      ingested_at     = now()
                    RETURNING (xmax = 0) AS is_new
                """, (
                    _SOURCE_TAG, str(recall_no), mfr_id,
                    defect, remedy,
                    report_date, "KR",
                    json.dumps(item, ensure_ascii=False),
                    report_date,
                ))
                is_new = cur.fetchone()[0]
                cur.execute("RELEASE SAVEPOINT sp_dg_recall")
                if is_new:
                    inserted += 1
                else:
                    updated += 1
            except Exception as exc:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_dg_recall")
                log.warning("[load:datagokr_recalls] %s/%s 실패: %s",
                            filename, recall_no, exc)
                skipped += 1

    conn.commit()
    log.info("[load:datagokr_recalls] inserted=%d updated=%d skipped=%d",
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
