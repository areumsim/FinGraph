"""PG (auto.*) → Neo4j MERGE 동기화 — AutoGraph 그래프 적재.

원칙:
- PG 가 SSOT. Neo4j 는 관계 탐색용 derived view.
- 모든 관계 엣지에 source_id / source_type / confidence_score / validated_status /
  snapshot_year 동봉.
- 데이터가 부족한 관계(부품·공급사·공법)는 절대 생성하지 않거나 candidate 로만.

노드:
    Manufacturer  : id (== manufacturer_id), name, name_norm, country, wikidata_qid
    VehicleModel  : id (== model_id), name, name_norm, market, wikidata_qid
    VehicleVariant: id (== variant_id), model_year, trim, fuel_type, body_class
    Recall        : id (== recall_id), source, source_recall_no, report_date,
                    component_text, summary
    System        : code (예 "ENGINE"), name
    Component     : id (== component_id), name, system_code

관계:
    (Manufacturer)-[:MANUFACTURES]->(VehicleModel)
    (VehicleModel)-[:HAS_VARIANT]->(VehicleVariant)
    (VehicleVariant)-[:AFFECTED_BY]->(Recall)
    (VehicleModel)-[:AFFECTED_BY]->(Recall)              -- variant 매핑 실패 시 fallback
    (Component)-[:CONTAINED_IN]->(System)
    (VehicleModel)-[:CONTAINS_COMPONENT]->(Component)    -- candidate 만 (Wikidata 등)
    (Component)-[:SUPPLIED_BY]->(Supplier)               -- candidate 만

CLI:
    python -m autograph.loaders.load_auto_neo4j --batch 500
"""

from __future__ import annotations

import argparse
import logging

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection


log = logging.getLogger(__name__)


# ── 노드 MERGE ──────────────────────────────────────────────
MERGE_MFR = """
UNWIND $rows AS r
MERGE (m:Manufacturer {id: r.id})
SET   m.name = r.name,
      m.name_norm = r.name_norm,
      m.country = coalesce(r.country, m.country),
      m.wikidata_qid = coalesce(r.wikidata_qid, m.wikidata_qid),
      m.source = r.source,
      m.snapshot_year = r.snapshot_year,
      m.updated_at = datetime()
"""

MERGE_MODEL = """
UNWIND $rows AS r
MATCH (m:Manufacturer {id: r.manufacturer_id})
MERGE (v:VehicleModel {id: r.id})
SET   v.name = r.name,
      v.name_norm = r.name_norm,
      v.market = r.market,
      v.wikidata_qid = coalesce(r.wikidata_qid, v.wikidata_qid),
      v.source = r.source,
      v.snapshot_year = r.snapshot_year,
      v.updated_at = datetime()
MERGE (m)-[rel:MANUFACTURES]->(v)
SET   rel.source_id = r.source,
      rel.source_type = 'pg.auto.master_vehicle_models',
      rel.confidence_score = r.confidence,
      rel.validated_status = r.validated_status,
      rel.snapshot_year = r.snapshot_year
"""

MERGE_VARIANT = """
UNWIND $rows AS r
MATCH (m:VehicleModel {id: r.model_id})
MERGE (v:VehicleVariant {id: r.id})
SET   v.model_year = r.model_year,
      v.trim = r.trim,
      v.fuel_type = r.fuel_type,
      v.body_class = r.body_class,
      v.source = r.source,
      v.snapshot_year = r.snapshot_year,
      v.updated_at = datetime()
MERGE (m)-[rel:HAS_VARIANT]->(v)
SET   rel.source_id = r.source,
      rel.source_type = 'pg.auto.master_vehicle_variants',
      rel.confidence_score = r.confidence,
      rel.validated_status = r.validated_status,
      rel.snapshot_year = r.snapshot_year
"""

MERGE_RECALL = """
UNWIND $rows AS r
MERGE (rc:Recall {id: r.id})
SET   rc.source = r.source,
      rc.source_recall_no = r.source_recall_no,
      rc.report_date = r.report_date,
      rc.country = r.country,
      rc.component_text = r.component_text,
      rc.summary = r.defect_summary,
      rc.consequence = r.consequence,
      rc.remedy = r.remedy_summary,
      rc.affected_units = r.affected_units,
      rc.snapshot_year = r.snapshot_year,
      rc.updated_at = datetime()
WITH rc, r
FOREACH (_ IN CASE WHEN r.variant_id IS NULL THEN [] ELSE [1] END |
  MERGE (v:VehicleVariant {id: r.variant_id})
  MERGE (v)-[rel:AFFECTED_BY]->(rc)
  SET   rel.source_id = r.source_recall_no,
        rel.source_type = 'pg.auto.events_recalls',
        rel.confidence_score = r.confidence,
        rel.validated_status = r.validated_status,
        rel.snapshot_year = r.snapshot_year
)
FOREACH (_ IN CASE WHEN r.model_id IS NULL OR r.variant_id IS NOT NULL THEN []
                   ELSE [1] END |
  MERGE (m:VehicleModel {id: r.model_id})
  MERGE (m)-[rel:AFFECTED_BY]->(rc)
  SET   rel.source_id = r.source_recall_no,
        rel.source_type = 'pg.auto.events_recalls',
        rel.confidence_score = r.confidence,
        rel.validated_status = r.validated_status,
        rel.snapshot_year = r.snapshot_year
)
"""

MERGE_COMPONENT = """
UNWIND $rows AS r
MERGE (c:Component {id: r.id})
SET   c.name = r.canonical_name,
      c.name_norm = r.name_norm,
      c.system_code = r.system_code,
      c.wikidata_qid = r.wikidata_qid,
      c.source = r.source,
      c.updated_at = datetime()
WITH c, r
MERGE (s:System {code: r.system_code})
MERGE (c)-[rel:CONTAINED_IN]->(s)
SET   rel.source_id = 'auto.components',
      rel.source_type = 'pg.auto.components',
      rel.confidence_score = 1.0,
      rel.validated_status = 'verified',
      rel.snapshot_year = r.snapshot_year
"""

# bridge.corp_entity 의 supplier entity → Neo4j Supplier 노드. 관계 (SUPPLIED_BY) 는
# 어떤 (vehicle, component) 가 어느 supplier 에 의존하는지 Wikidata P176 등으로 알아야
# 그릴 수 있어 본 loader 에서는 노드만 적재. corp_code 매칭 정보 (lei/business_no/qid)
# 가 있는 supplier 만 노드로 — 단순 corp_entity row 가 매핑 정보 충분할 때만.
MERGE_SUPPLIER = """
UNWIND $rows AS r
MERGE (sup:Supplier {wikidata_qid: r.wikidata_qid})
SET   sup.name = r.name,
      sup.corp_code = r.corp_code,
      sup.reviewed_status = r.reviewed_status,
      sup.confidence_score = r.confidence_score,
      sup.match_method = r.match_method,
      sup.updated_at = datetime()
"""


def _fetch_mfr(cur) -> list[dict]:
    cur.execute("""
        SELECT manufacturer_id, name, name_norm, country, wikidata_qid,
               source, confidence, validated_status, snapshot_year
          FROM auto.master_manufacturers
    """)
    return [{
        "id": r[0], "name": r[1], "name_norm": r[2], "country": r[3],
        "wikidata_qid": r[4], "source": r[5], "confidence": float(r[6]),
        "validated_status": r[7], "snapshot_year": r[8],
    } for r in cur.fetchall()]


def _fetch_models(cur) -> list[dict]:
    cur.execute("""
        SELECT model_id, manufacturer_id, name, name_norm, market, wikidata_qid,
               source, confidence, validated_status, snapshot_year
          FROM auto.master_vehicle_models
    """)
    return [{
        "id": r[0], "manufacturer_id": r[1], "name": r[2], "name_norm": r[3],
        "market": r[4], "wikidata_qid": r[5], "source": r[6],
        "confidence": float(r[7]), "validated_status": r[8], "snapshot_year": r[9],
    } for r in cur.fetchall()]


def _fetch_variants(cur) -> list[dict]:
    cur.execute("""
        SELECT variant_id, model_id, model_year, trim, fuel_type, body_class,
               source, confidence, validated_status, snapshot_year
          FROM auto.master_vehicle_variants
    """)
    return [{
        "id": r[0], "model_id": r[1], "model_year": r[2], "trim": r[3],
        "fuel_type": r[4], "body_class": r[5], "source": r[6],
        "confidence": float(r[7]), "validated_status": r[8], "snapshot_year": r[9],
    } for r in cur.fetchall()]


def _fetch_recalls(cur) -> list[dict]:
    cur.execute("""
        SELECT recall_id, source, source_recall_no, manufacturer_id, model_id,
               variant_id, component_text, defect_summary, consequence,
               remedy_summary, report_date, country, affected_units,
               confidence, validated_status, snapshot_year
          FROM auto.events_recalls
    """)
    rows = []
    for r in cur.fetchall():
        rows.append({
            "id": r[0], "source": r[1], "source_recall_no": r[2],
            "manufacturer_id": r[3], "model_id": r[4], "variant_id": r[5],
            "component_text": r[6], "defect_summary": r[7], "consequence": r[8],
            "remedy_summary": r[9],
            "report_date": r[10].isoformat() if r[10] else None,
            "country": r[11], "affected_units": r[12],
            "confidence": float(r[13]), "validated_status": r[14],
            "snapshot_year": r[15],
        })
    return rows


def _fetch_components(cur) -> list[dict]:
    cur.execute("""
        SELECT component_id, canonical_name, name_norm, system_code,
               wikidata_qid, source
          FROM auto.components
    """)
    return [{
        "id": r[0], "canonical_name": r[1], "name_norm": r[2],
        "system_code": r[3], "wikidata_qid": r[4], "source": r[5],
        "snapshot_year": None,
    } for r in cur.fetchall()]


def _fetch_suppliers(cur) -> list[dict]:
    """bridge.corp_entity 의 entity_type='supplier' row 를 Neo4j Supplier 노드용 dict 로."""
    cur.execute("""
        SELECT entity_id AS wikidata_qid, name, corp_code,
               reviewed_status, confidence_score, match_method
          FROM bridge.corp_entity
         WHERE entity_type = 'supplier'
           AND reviewed_status <> 'rejected'
    """)
    return [{
        "wikidata_qid": r[0], "name": r[1], "corp_code": r[2],
        "reviewed_status": r[3], "confidence_score": float(r[4]),
        "match_method": r[5],
    } for r in cur.fetchall()]


def _run_batched(session, cypher: str, rows: list[dict], batch: int) -> int:
    n = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        if not chunk:
            continue
        session.run(cypher, rows=chunk)
        n += len(chunk)
    return n


def load_all(batch: int = 500) -> dict:
    out: dict = {}
    pg = get_connection()
    with pg.cursor() as cur:
        mfr = _fetch_mfr(cur)
        models = _fetch_models(cur)
        variants = _fetch_variants(cur)
        recalls = _fetch_recalls(cur)
        components = _fetch_components(cur)
        suppliers = _fetch_suppliers(cur)
    pg.commit()

    driver = get_driver()
    with driver.session() as session:
        out["manufacturers"] = _run_batched(session, MERGE_MFR, mfr, batch)
        out["models"]        = _run_batched(session, MERGE_MODEL, models, batch)
        out["variants"]      = _run_batched(session, MERGE_VARIANT, variants, batch)
        out["recalls"]       = _run_batched(session, MERGE_RECALL, recalls, batch)
        out["components"]    = _run_batched(session, MERGE_COMPONENT, components, batch)
        out["suppliers"]     = _run_batched(session, MERGE_SUPPLIER, suppliers, batch)

    log.info("[neo4j] loaded %s", out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_neo4j")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_all(batch=args.batch)


if __name__ == "__main__":
    main()
