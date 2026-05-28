"""KNCAP raw → auto.spec_measurements (safety.kncap.*) + Neo4j SAFETY_RATED_BY.

raw 위치: ``data/raw/auto/kncap/*.jsonl`` (ingestion.kncap 가 normalize 후 생성)

raw 부재 또는 :Standard {code:'KNCAP'} 노드 미적재 시 graceful skip.

CLI:
    python -m autograph.loaders.load_kncap
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from autonexusgraph.config import get_settings
from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name

from ..loaders._neo4j_helpers import edge_meta_cypher, run_batched


log = logging.getLogger(__name__)


_SOURCE_PATH = "auto/kncap"
_DEFAULT_CONFIDENCE = 0.90
_EXTRACTOR_NAME = "kncap_loader"
_EXTRACTOR_VERSION = "v1"


_MERGE_CYPHER = f"""
UNWIND $rows AS r
MATCH (v:VehicleVariant {{variant_id: r.variant_id}})
MATCH (s:Standard {{code: 'KNCAP'}})
MERGE (v)-[edge:SAFETY_RATED_BY]->(s)
SET {edge_meta_cypher('edge')},
    edge.overall_rating = r.overall_rating,
    edge.test_year = r.test_year
"""


def _resolve_variant_id(cur, manufacturer: str | None, model: str | None,
                       year: int | None) -> int | None:
    if not (manufacturer and model and year):
        return None
    mn = normalize_corp_name(manufacturer)
    mod = normalize_corp_name(model)
    cur.execute("""
        SELECT v.variant_id
          FROM auto.master_vehicle_variants v
          JOIN auto.master_vehicle_models m ON m.model_id = v.model_id
          JOIN auto.master_manufacturers mf ON mf.manufacturer_id = m.manufacturer_id
         WHERE mf.name_norm = %s
           AND m.name_norm = %s
           AND v.model_year = %s
         LIMIT 1
    """, (mn, mod, year))
    r = cur.fetchone()
    return r[0] if r else None


def run(*, dry_run: bool = False) -> dict:
    raw_root = get_settings().ingest_raw_dir / _SOURCE_PATH
    if not raw_root.exists():
        log.warning("[load:kncap] %s 없음 — graceful skip", raw_root)
        return {"variants": 0, "edges": 0}

    jsonls = list(raw_root.glob("*.jsonl"))
    if not jsonls:
        log.warning("[load:kncap] jsonl 없음 — graceful skip")
        return {"variants": 0, "edges": 0}

    conn = get_connection()
    pg_rows: list[dict] = []
    with conn.cursor() as cur:
        for j in sorted(jsonls):
            for line in j.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                year_raw = row.get("model_year") or row.get("test_date") or ""
                try:
                    year = int(str(year_raw)[:4])
                except ValueError:
                    year = None
                vid = _resolve_variant_id(
                    cur,
                    row.get("manufacturer_kr"),
                    row.get("model_kr"),
                    year,
                )
                if vid is None:
                    continue
                # PG: spec_measurements (safety.kncap.*) UPSERT.
                if not dry_run:
                    for key in ("overall_rating", "frontal_impact",
                                "side_impact", "rollover"):
                        val = row.get(key)
                        if val is None or val == "":
                            continue
                        cur.execute("""
                            INSERT INTO auto.spec_measurements
                              (variant_id, measure_key, value_text, source, snapshot_year)
                            VALUES (%s, %s, %s, 'kncap', %s)
                            ON CONFLICT (variant_id, measure_key)
                            DO UPDATE SET value_text = EXCLUDED.value_text,
                                          source     = EXCLUDED.source
                        """, (vid, f"safety.kncap.{key}", str(val), year))
                pg_rows.append({
                    "variant_id":       vid,
                    "overall_rating":   row.get("overall_rating"),
                    "test_year":        year,
                    "source_type":      "kncap",
                    "source_id":        f"kncap/{j.name}",
                    "confidence_score": _DEFAULT_CONFIDENCE,
                    "validated_status": "validated",
                    "snapshot_year":    year,
                    "extraction_method": "deterministic",
                })

    if not dry_run:
        conn.commit()
    log.info("[load:kncap] PG safety.kncap.* upserts done. variants=%d", len(pg_rows))

    if dry_run:
        return {"variants": len(pg_rows), "edges": 0}

    driver = get_driver()
    with driver.session() as session:
        edges = run_batched(session, _MERGE_CYPHER, pg_rows, batch=200)
    log.info("[load:kncap] Neo4j SAFETY_RATED_BY edges=%d", edges)
    return {"variants": len(pg_rows), "edges": edges}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run"]
