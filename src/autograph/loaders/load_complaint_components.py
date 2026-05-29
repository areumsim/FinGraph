"""events_complaints.components → Neo4j :Complaint-[:COMPLAINT_OF]->:Module 매핑.

NHTSA complaint 의 ``components`` 컬럼은 string array (예: ``['ELECTRICAL SYSTEM']``).
각 component string 을 auto.components.canonical_name (NHTSA taxonomy loader 가
적재한 것) 과 정확/별칭 매칭 후 Neo4j edge 생성:

    (:Complaint {id: N})-[:COMPLAINT_OF {meta...}]->(:Module|:Component {name})

선행 필요: ``python -m autograph.loaders.load_nhtsa_component_taxonomy``.

PRD §6.7 의무 메타 6개 모두 채움. PRD §3.5 C 등급 (사용자 신고) → confidence=0.50.

CLI:
    python -m autograph.loaders.load_complaint_components
    python -m autograph.loaders.load_complaint_components --dry-run
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection

from ._text_utils import norm_text as _norm


log = logging.getLogger(__name__)


@dataclass
class MatchStats:
    complaints_scanned: int = 0
    edges_created:      int = 0
    no_match:           int = 0
    errors: list[str]   = field(default_factory=list)


_MERGE_COMPLAINT_OF = """
UNWIND $rows AS row
MATCH (cp:Complaint {id: row.complaint_id})
MATCH (m {name_norm: row.comp_norm})
WHERE (m:Module OR m:Component)
MERGE (cp)-[r:COMPLAINT_OF]->(m)
ON CREATE SET r.source_type      = 'nhtsa_complaint',
              r.source_id        = row.source_id,
              r.confidence_score = row.confidence,
              r.validated_status = 'candidate',
              r.snapshot_year    = row.snapshot_year,
              r.extraction_method = 'exact_match_taxonomy',
              r.created_at       = datetime()
"""

_CONFIDENCE = 0.50   # PRD §3.5 C 등급.


def load_complaint_components(*, dry_run: bool = False,
                                batch: int = 1000) -> MatchStats:
    stats = MatchStats()
    conn = get_connection()
    rows: list[dict] = []

    with conn.cursor() as cur:
        # 1) component_name → norm 매핑 (Neo4j 측 노드 매칭용 키).
        cur.execute("""
            SELECT canonical_name, name_norm FROM auto.components
        """)
        canonical_norms = {c[1] for c in cur.fetchall()}

        # 2) 모든 complaint 의 components 배열 unnest.
        cur.execute("""
            SELECT complaint_id, components, snapshot_year, source_complaint_no
              FROM auto.events_complaints
             WHERE components IS NOT NULL AND array_length(components, 1) > 0
        """)
        for cid, comps, snap, src_no in cur.fetchall():
            stats.complaints_scanned += 1
            matched_any = False
            for raw in comps:
                cn = _norm(str(raw))
                if not cn:
                    continue
                if cn not in canonical_norms:
                    continue
                rows.append({
                    "complaint_id":  int(cid),
                    "comp_norm":     cn,
                    "source_id":     str(src_no or cid),
                    "confidence":    _CONFIDENCE,
                    "snapshot_year": int(snap) if snap else None,
                })
                matched_any = True
            if not matched_any:
                stats.no_match += 1

    if dry_run:
        log.info("[complaint→comp] DRY-RUN scanned=%d would_edges=%d no_match=%d",
                 stats.complaints_scanned, len(rows), stats.no_match)
        return stats

    # 3) Neo4j MERGE.
    driver = get_driver()
    with driver.session() as s:
        for i in range(0, len(rows), batch):
            chunk = rows[i: i + batch]
            try:
                s.run(_MERGE_COMPLAINT_OF, rows=chunk).consume()
                stats.edges_created += len(chunk)
            except Exception as e:   # noqa: BLE001
                stats.errors.append(f"chunk[{i}]: {e}")

    log.info(
        "[complaint→comp] scanned=%d edges=%d no_match=%d errors=%d",
        stats.complaints_scanned, stats.edges_created,
        stats.no_match, len(stats.errors),
    )
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_complaint_components")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_complaint_components(dry_run=args.dry_run, batch=args.batch)


if __name__ == "__main__":
    main()


__all__ = ["load_complaint_components", "MatchStats", "_norm"]
