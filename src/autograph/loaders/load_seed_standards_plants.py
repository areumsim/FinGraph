"""(:Standard) + (:Plant) 시드 적재.

본 PR 의 1차 source 는 ontology/auto/standards.yaml + plants.yaml. 즉시 사용 가능한
~22 표준 + ~18 공장 노드를 그래프에 노출. 차량↔표준 (COMPLIES_WITH / SAFETY_RATED_BY)
및 모델↔공장 (MANUFACTURED_AT) 의 구체 엣지는 KNCAP/KATRI/IR 수집 PR 에서 추가.

본 모듈이 만드는 것:
  • :Standard {code, name, region, agency, url}
  • :Plant {code, name, country, city, wikidata_qid}
  • (:Manufacturer)-[:OWNS_PLANT]->(:Plant) — manufacturer_name 으로 OEM 매칭 시.
    (OWNS_PLANT 는 plants.yaml seed 의 자연스러운 산출 — MANUFACTURED_AT 보다 보수적.)
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.ingestion._common import normalize_corp_name

from ..ontology import load_plants, load_standards, load_system_taxonomy
from ._neo4j_helpers import run_batched


log = logging.getLogger(__name__)


@dataclass
class LoadStats:
    systems: int = 0
    standards: int = 0
    plants:    int = 0
    owns_plant_edges: int = 0
    errors: list[str]  = field(default_factory=list)


# ── System (taxonomy seed) — load_auto_neo4j 도 적재하지만 본 시드 단독 실행 가능. ──
_MERGE_SYSTEM = """
UNWIND $rows AS r
MERGE (s:System {code: r.code})
SET   s.name = r.name,
      s.description = r.description,
      s.updated_at = datetime()
"""


# ── Standard ────────────────────────────────────────────────────
_MERGE_STANDARD = """
UNWIND $rows AS r
MERGE (s:Standard {code: r.code})
SET   s.name = r.name,
      s.region = r.region,
      s.agency = r.agency,
      s.url = r.url,
      s.updated_at = datetime()
"""


# ── Plant + OWNS_PLANT 엣지 ─────────────────────────────────────
_MERGE_PLANT = """
UNWIND $rows AS r
MERGE (p:Plant {code: r.code})
SET   p.name = r.name,
      p.country = r.country,
      p.city = r.city,
      p.wikidata_qid = r.wikidata_qid,
      p.updated_at = datetime()
WITH p, r
OPTIONAL MATCH (mm:Manufacturer)
  WHERE r.mfr_name_norm IS NOT NULL AND mm.name_norm = r.mfr_name_norm
WITH p, r, mm WHERE mm IS NOT NULL
MERGE (mm)-[rel:OWNS_PLANT]->(p)
SET   rel.source_type      = 'plants_seed',
      rel.source_id        = r.code,
      rel.extraction_method= 'manual',
      rel.confidence_score = 0.95,
      rel.validated_status = 'validated',
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year)
"""


def load_seed_standards_plants(*, batch: int = 200) -> LoadStats:
    stats = LoadStats()
    systems = [
        {"code": code, "name": row["name"], "description": row.get("description")}
        for code, row in load_system_taxonomy().items()
    ]
    standards = [
        {
            "code": r["code"], "name": r.get("name"),
            "region": r.get("region"), "agency": r.get("agency"),
            "url": r.get("url"),
        }
        for r in load_standards()
    ]
    plants = [
        {
            "code": r["code"], "name": r.get("name"),
            "country": r.get("country"), "city": r.get("city"),
            "wikidata_qid": r.get("wikidata_qid"),
            "mfr_name_norm": normalize_corp_name(r["manufacturer_name"])
              if r.get("manufacturer_name") else None,
            "snapshot_year": None,
        }
        for r in load_plants()
    ]

    driver = get_driver()
    with driver.session() as session:
        stats.systems   = run_batched(session, _MERGE_SYSTEM,   systems,   batch=batch)
        stats.standards = run_batched(session, _MERGE_STANDARD, standards, batch=batch)
        stats.plants    = run_batched(session, _MERGE_PLANT,    plants,    batch=batch)
        # OWNS_PLANT 엣지 수는 별도 카운트 — UNWIND 안에서 RETURN 없이 SET 만.
        # 매뉴팩처러 매칭 실패 시 엣지는 안 생기지만 노드는 생성됨.

    log.info("[seed] systems=%d standards=%d plants=%d",
             stats.systems, stats.standards, stats.plants)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_seed_standards_plants")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_seed_standards_plants(batch=args.batch)


if __name__ == "__main__":
    main()


__all__ = ["load_seed_standards_plants", "LoadStats"]
