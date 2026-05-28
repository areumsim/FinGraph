"""(VehicleModel)-[:CONTAINS_SYSTEM]->(System) — derived 1-hop edge.

이 엣지는 외부 데이터 없이 그래프 내부에서 derive:
    (VehicleModel)-[:CONTAINS_COMPONENT]->(Module|Part)-[:CONTAINED_IN*1..2]->(System)
→  (VehicleModel)-[:CONTAINS_SYSTEM]->(System)

이미 적재된 CONTAINS_COMPONENT (AI-Hub deterministic + LLM 후보) 와 CONTAINED_IN
(System 계층 시드) 만 있으면 자동으로 derive 가능. main_hop 쿼리 단축 — 사용자 질문
"Tesla Model Y 는 어떤 시스템들을 갖나?" 가 1-hop 으로 답 가능.

confidence_score: 출처 엣지 (CONTAINS_COMPONENT) 의 confidence 평균 또는 max.
                  본 모듈은 max — 한 System 안에 신뢰도 높은 Module 이 하나라도 있으면
                  그 System 자체는 신뢰 가능.

source_type: 'derived/contains_component+contained_in'
extraction_method: 'derived'
validated_status: 'verified' (deterministic 계산이므로)

CLI:
    python -m autograph.loaders.derive_contains_system
    python -m autograph.loaders.derive_contains_system --dry-run

선행 조건: load_auto_neo4j (System 노드) + load_auto_aihub (CONTAINS_COMPONENT) +
load_seed_standards_plants (System 시드 백업).
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from autonexusgraph.db.neo4j import get_driver


log = logging.getLogger(__name__)


@dataclass
class DeriveStats:
    pairs_seen:    int = 0      # (model, system) 후보 쌍 수
    edges_merged:  int = 0
    errors: list[str] = field(default_factory=list)


# (m:VehicleModel)-[r1:CONTAINS_COMPONENT]->(c:Module|Part) -> (s:System)
# c 가 Part 인 경우 Module 한 단계 더 거침 (CONTAINED_IN*1..2 로 양쪽 수용).
# 같은 (model, system) 쌍의 중복은 WITH DISTINCT + MERGE 가 처리.
_DERIVE_CYPHER = """
MATCH (m:VehicleModel)-[r1:CONTAINS_COMPONENT]->(c)
WHERE c:Module OR c:Part
MATCH (c)-[:CONTAINED_IN*1..2]->(s:System)
WITH m, s,
     max(coalesce(r1.confidence_score, 0.0))      AS max_conf,
     min(coalesce(r1.snapshot_year, date().year)) AS min_year,
     collect(DISTINCT r1.source_type)[..5]        AS src_types,
     count(DISTINCT c)                            AS support_n
MERGE (m)-[rel:CONTAINS_SYSTEM]->(s)
SET   rel.source_type        = 'derived/contains_component+contained_in',
      rel.source_id           = 'derive_contains_system',
      rel.extraction_method   = 'derived',
      rel.confidence_score    = max_conf,
      rel.validated_status    = 'verified',
      rel.snapshot_year       = min_year,
      rel.support_n           = support_n,
      rel.support_source_types = src_types
RETURN count(rel) AS n
"""

# Dry-run 미리보기 — MERGE 없이 후보 쌍 카운트만.
_PREVIEW_CYPHER = """
MATCH (m:VehicleModel)-[r1:CONTAINS_COMPONENT]->(c)
WHERE c:Module OR c:Part
MATCH (c)-[:CONTAINED_IN*1..2]->(s:System)
RETURN count(DISTINCT [m.id, s.code]) AS pairs
"""


def derive_contains_system(*, dry_run: bool = False) -> DeriveStats:
    stats = DeriveStats()
    driver = get_driver()
    with driver.session() as session:
        # 후보 쌍 미리보기.
        rec = session.run(_PREVIEW_CYPHER).single()
        stats.pairs_seen = int(rec["pairs"]) if rec else 0
        log.info("[derive:contains_system] %d candidate (model, system) pairs",
                 stats.pairs_seen)

        if dry_run:
            log.info("[derive:contains_system] DRY-RUN — no MERGE executed")
            return stats

        # 실제 MERGE.
        rec = session.run(_DERIVE_CYPHER).single()
        stats.edges_merged = int(rec["n"]) if rec else 0

    log.info("[derive:contains_system] merged=%d edges (pairs_seen=%d)",
             stats.edges_merged, stats.pairs_seen)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.derive_contains_system")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    derive_contains_system(dry_run=args.dry_run)


if __name__ == "__main__":
    main()


__all__ = ["derive_contains_system", "DeriveStats"]
