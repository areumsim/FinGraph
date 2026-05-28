"""auto.events_complaints → :Complaint + (:VehicleVariant)-[:REPORTED_IN]->(:Complaint).

Recall 적재 패턴과 동일: 1) :Complaint 노드 MERGE, 2) variant 가 PG 에서 매칭된 경우만
REPORTED_IN 엣지. 매칭 안 된 complaint 는 노드만 남기고 그래프 멀티홉에서는 빠진다.

CLI:
    python -m autograph.loaders.load_complaints_neo4j
    python -m autograph.loaders.load_complaints_neo4j --dry-run
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection

from ._neo4j_helpers import run_batched


log = logging.getLogger(__name__)


_MERGE_COMPLAINT = """
UNWIND $rows AS r
MERGE (cmp:Complaint {id: r.id})
SET   cmp.source = r.source,
      cmp.source_complaint_no = r.source_complaint_no,
      cmp.filed_date = r.filed_date,
      cmp.incident_date = r.incident_date,
      cmp.country = r.country,
      cmp.summary = r.summary,
      cmp.components_text = r.components_text,
      cmp.snapshot_year = r.snapshot_year,
      cmp.updated_at = datetime()
"""


_MERGE_REPORTED_IN = """
UNWIND $rows AS r
MATCH (cmp:Complaint {id: r.id})
WITH cmp, r WHERE r.variant_id IS NOT NULL
OPTIONAL MATCH (v:VehicleVariant {id: r.variant_id})
WITH cmp, r, v WHERE v IS NOT NULL
MERGE (v)-[rel:REPORTED_IN]->(cmp)
SET   rel.source_type      = 'pg.auto.events_complaints',
      rel.source_id        = r.source_complaint_no,
      rel.extraction_method= 'deterministic',
      rel.confidence_score = 1.0,
      rel.validated_status = 'verified',
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year)
"""


@dataclass
class LoadStats:
    complaints: int = 0
    edges:      int = 0
    errors: list[str] = field(default_factory=list)


def _fetch_complaints(cur) -> list[dict]:
    cur.execute("""
        SELECT complaint_id, source, source_complaint_no, variant_id, model_id,
               manufacturer_id, components, summary, filed_date, incident_date,
               country, snapshot_year
          FROM auto.events_complaints
    """)
    rows: list[dict] = []
    for r in cur.fetchall():
        rows.append({
            "id": r[0], "source": r[1], "source_complaint_no": r[2],
            "variant_id": r[3], "model_id": r[4], "manufacturer_id": r[5],
            "components_text": ", ".join(r[6] or []),
            "summary": r[7],
            "filed_date":    r[8].isoformat()  if r[8]  else None,
            "incident_date": r[9].isoformat()  if r[9]  else None,
            "country": r[10], "snapshot_year": r[11],
        })
    return rows


def load_complaints_neo4j(*, batch: int = 500) -> LoadStats:
    stats = LoadStats()
    conn = get_connection()
    with conn.cursor() as cur:
        rows = _fetch_complaints(cur)
    conn.commit()

    if not rows:
        log.info("[complaints→neo4j] PG 비어있음 — skip")
        return stats

    driver = get_driver()
    with driver.session() as session:
        stats.complaints = run_batched(session, _MERGE_COMPLAINT,  rows, batch=batch)
        run_batched(session, _MERGE_REPORTED_IN, rows, batch=batch)
        # 엣지 카운트 측정 — REPORTED_IN 의 수는 RETURN 없이 SET 만으로 추적 불편.
        # 별도 쿼리로 후집계.
        rec = session.run(
            "MATCH ()-[r:REPORTED_IN]->() RETURN count(r) AS n"
        ).single()
        stats.edges = int(rec["n"]) if rec else 0

    log.info("[complaints→neo4j] complaints=%d total_REPORTED_IN=%d",
             stats.complaints, stats.edges)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_complaints_neo4j")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_complaints_neo4j(batch=args.batch)


if __name__ == "__main__":
    main()


__all__ = ["load_complaints_neo4j", "LoadStats"]
