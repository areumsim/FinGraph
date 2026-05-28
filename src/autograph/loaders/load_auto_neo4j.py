"""PG (auto.*) → Neo4j MERGE 동기화 — AutoGraph 그래프 적재.

원칙:
- PG 가 SSOT. Neo4j 는 관계 탐색용 derived view.
- 모든 관계 엣지에 §6.7 의무 메타 동봉 (source_id, source_type, confidence_score,
  validated_status, snapshot_year, extraction_method).
- 데이터가 부족한 관계는 절대 생성하지 않거나 confidence < 1.0 candidate 로만.

노드 (라벨 SSOT 는 ontology/auto/entities.yaml):
    Manufacturer  : {id}    — auto.master_manufacturers.manufacturer_id
    VehicleModel  : {id}    — auto.master_vehicle_models.model_id
    VehicleVariant: {id}    — auto.master_vehicle_variants.variant_id
    Recall        : {id}    — auto.events_recalls.recall_id
    System        : {code}  — canonical SCREAMING_SNAKE (system_taxonomy.yaml)
    Module        : {id}    — auto.components.component_id (level=4)
    Part          : {id}    — auto.components.component_id (level=5)
    Supplier      : {entity_id} — stringified auto.master_suppliers.supplier_id

관계 (관계 타입 SSOT 는 ontology/auto/relations.yaml):
    (Manufacturer)-[:MANUFACTURES]->(VehicleModel)
    (VehicleModel)-[:HAS_VARIANT]->(VehicleVariant)
    (VehicleVariant)-[:AFFECTED_BY]->(Recall)
    (VehicleModel)-[:AFFECTED_BY]->(Recall)              -- variant 매핑 실패 시 fallback
    (Module)-[:CONTAINED_IN]->(System)
    (Part)-[:CONTAINED_IN]->(Module)

본 모듈이 적재하지 않는 엣지 (각자 별도 로더):
    (Recall)-[:RECALL_OF]->(Module|Part)            → load_recall_components.py
    (Module|Part)-[:SUPPLIED_BY]->(Supplier)         → load_supplier_edges.py
    (VehicleModel)-[:MANUFACTURED_AT]->(Plant)       → load_seed_standards_plants.py
    (VehicleVariant)-[:COMPLIES_WITH]->(Standard)    → load_seed_standards_plants.py
    (VehicleVariant)-[:REPORTED_IN]->(Complaint)     → load_complaints_neo4j.py
    (VehicleModel)-[:CONTAINS_COMPONENT]->(Component) → load_auto_aihub.py (AI-Hub 매핑)

CLI:
    python -m autograph.loaders.load_auto_neo4j --batch 500
"""

from __future__ import annotations

import argparse
import logging

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection

from ..ontology import canonical_system_code, load_system_taxonomy
from ._neo4j_helpers import run_batched


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
      rel.extraction_method = 'deterministic',
      rel.confidence_score = r.confidence,
      rel.validated_status = r.validated_status,
      rel.snapshot_year = coalesce(r.snapshot_year, date().year)
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
      rel.extraction_method = 'deterministic',
      rel.confidence_score = r.confidence,
      rel.validated_status = r.validated_status,
      rel.snapshot_year = coalesce(r.snapshot_year, date().year)
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
"""

# (VehicleVariant)-[:AFFECTED_BY]->(Recall) — variant_id 가 PG 에서 매칭된 경우에만 엣지.
# OPTIONAL MATCH 로 실제 variant 노드가 있을 때만 연결 — 미매칭 recall 은 다음 fallback 패스로.
MERGE_RECALL_EDGE_VARIANT = """
UNWIND $rows AS r
MATCH (rc:Recall {id: r.id})
WITH rc, r WHERE r.variant_id IS NOT NULL
OPTIONAL MATCH (v:VehicleVariant {id: r.variant_id})
WITH rc, r, v WHERE v IS NOT NULL
MERGE (v)-[rel:AFFECTED_BY]->(rc)
SET   rel.source_id = r.source_recall_no,
      rel.source_type = 'pg.auto.events_recalls',
      rel.extraction_method = 'deterministic',
      rel.confidence_score = r.confidence,
      rel.validated_status = r.validated_status,
      rel.snapshot_year = coalesce(r.snapshot_year, date().year)
"""

# (VehicleModel)-[:AFFECTED_BY]->(Recall) — variant 매핑이 실패했을 때의 fallback.
MERGE_RECALL_EDGE_MODEL_FALLBACK = """
UNWIND $rows AS r
MATCH (rc:Recall {id: r.id})
WITH rc, r WHERE r.variant_id IS NULL AND r.model_id IS NOT NULL
OPTIONAL MATCH (m:VehicleModel {id: r.model_id})
WITH rc, r, m WHERE m IS NOT NULL
MERGE (m)-[rel:AFFECTED_BY]->(rc)
SET   rel.source_id = r.source_recall_no,
      rel.source_type = 'pg.auto.events_recalls',
      rel.extraction_method = 'deterministic',
      rel.confidence_score = r.confidence,
      rel.validated_status = r.validated_status,
      rel.snapshot_year = coalesce(r.snapshot_year, date().year)
"""

# Level 3 (System): 노드 + name + description. system_taxonomy 시드로 보강.
MERGE_SYSTEM = """
UNWIND $rows AS r
MERGE (s:System {code: r.code})
SET   s.name = coalesce(r.name, s.name),
      s.description = coalesce(r.description, s.description),
      s.updated_at = datetime()
"""

# Level 4 (Module): :Module 라벨 + (Module)-[:CONTAINED_IN]->(System).
MERGE_MODULE = """
UNWIND $rows AS r
MERGE (m:Module {id: r.id})
SET   m.name = r.canonical_name,
      m.name_norm = r.name_norm,
      m.system_code = r.system_code,
      m.wikidata_qid = r.wikidata_qid,
      m.source = r.source,
      m.updated_at = datetime()
WITH m, r
OPTIONAL MATCH (s:System {code: r.system_code})
WITH m, r, s WHERE s IS NOT NULL
MERGE (m)-[rel:CONTAINED_IN]->(s)
SET   rel.source_id = 'auto.components',
      rel.source_type = 'pg.auto.components',
      rel.extraction_method = 'deterministic',
      rel.confidence_score = 1.0,
      rel.validated_status = 'verified',
      rel.snapshot_year = coalesce(r.snapshot_year, date().year)
"""

# Level 5 (Part): :Part 라벨 + parent_component_id 가 있으면 (Part)-[:CONTAINED_IN]->(Module).
MERGE_PART = """
UNWIND $rows AS r
MERGE (p:Part {id: r.id})
SET   p.name = r.canonical_name,
      p.name_norm = r.name_norm,
      p.system_code = r.system_code,
      p.wikidata_qid = r.wikidata_qid,
      p.source = r.source,
      p.updated_at = datetime()
WITH p, r
OPTIONAL MATCH (parent:Module {id: r.parent_component_id})
WITH p, r, parent WHERE parent IS NOT NULL
MERGE (p)-[rel:CONTAINED_IN]->(parent)
SET   rel.source_id = 'auto.components',
      rel.source_type = 'pg.auto.components',
      rel.extraction_method = 'deterministic',
      rel.confidence_score = 1.0,
      rel.validated_status = 'verified',
      rel.snapshot_year = coalesce(r.snapshot_year, date().year)
"""

# auto.master_suppliers + bridge.corp_entity 의 supplier 행 → :Supplier 노드.
# entity_id (stringified supplier_id) 가 유일 키 — neo4j_init 제약과 일치.
# name_norm / country / wikidata_qid / corp_code 메타도 함께 세팅 (cross-domain bridge 진입점).
MERGE_SUPPLIER = """
UNWIND $rows AS r
MERGE (sup:Supplier {entity_id: r.entity_id})
SET   sup.name = r.name,
      sup.name_norm = r.name_norm,
      sup.country = r.country,
      sup.wikidata_qid = r.wikidata_qid,
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


def _fetch_systems_from_taxonomy() -> list[dict]:
    """ontology/auto/system_taxonomy.yaml → :System seed rows."""
    return [
        {"code": code, "name": row["name"], "description": row.get("description")}
        for code, row in load_system_taxonomy().items()
    ]


def _fetch_components_by_level(cur, level: int) -> list[dict]:
    """auto.components WHERE level=? → Module(4) / Part(5) dict 리스트.

    system_code 는 canonical_system_code() 로 정규화 (AI-Hub 'powertrain' → 'POWERTRAIN').
    """
    cur.execute("""
        SELECT component_id, canonical_name, name_norm, system_code,
               wikidata_qid, source, snapshot_year, parent_component_id
          FROM auto.components
         WHERE level = %s
    """, (level,))
    rows: list[dict] = []
    for r in cur.fetchall():
        rows.append({
            "id": r[0], "canonical_name": r[1], "name_norm": r[2],
            "system_code": canonical_system_code(r[3]),
            "wikidata_qid": r[4], "source": r[5],
            "snapshot_year": r[6],
            "parent_component_id": r[7],
        })
    return rows


def _fetch_suppliers(cur) -> list[dict]:
    """auto.master_suppliers + bridge.corp_entity 조인 → :Supplier 노드용 dict.

    entity_id = stringified supplier_id (SSOT — bridge.corp_entity 와 일치).
    corp_code 매핑이 있는 row 도 함께 (Cross-Domain Bridge 진입점).
    """
    cur.execute("""
        SELECT s.supplier_id, s.name, s.name_norm, s.country, s.wikidata_qid,
               be.corp_code, be.reviewed_status, be.confidence_score, be.match_method
          FROM auto.master_suppliers s
          LEFT JOIN bridge.corp_entity be
            ON be.entity_type = 'supplier'
           AND be.entity_id   = s.supplier_id::text
         WHERE COALESCE(be.reviewed_status, 'candidate') <> 'rejected'
    """)
    return [{
        "entity_id": str(r[0]), "name": r[1], "name_norm": r[2],
        "country": r[3], "wikidata_qid": r[4],
        "corp_code": r[5],
        "reviewed_status": r[6] or "candidate",
        "confidence_score": float(r[7]) if r[7] is not None else 0.80,
        "match_method": r[8],
    } for r in cur.fetchall()]


def load_all(batch: int = 500) -> dict:
    out: dict = {}
    pg = get_connection()
    with pg.cursor() as cur:
        mfr      = _fetch_mfr(cur)
        models   = _fetch_models(cur)
        variants = _fetch_variants(cur)
        recalls  = _fetch_recalls(cur)
        modules  = _fetch_components_by_level(cur, 4)
        parts    = _fetch_components_by_level(cur, 5)
        suppliers = _fetch_suppliers(cur)
    pg.commit()

    systems = _fetch_systems_from_taxonomy()

    driver = get_driver()
    with driver.session() as session:
        out["manufacturers"] = run_batched(session, MERGE_MFR, mfr, batch=batch)
        out["models"]        = run_batched(session, MERGE_MODEL, models, batch=batch)
        out["variants"]      = run_batched(session, MERGE_VARIANT, variants, batch=batch)
        # Recall: 노드 먼저, 그 다음 AFFECTED_BY 엣지 두 패스 (variant 우선, model fallback).
        out["recalls"]       = run_batched(session, MERGE_RECALL, recalls, batch=batch)
        run_batched(session, MERGE_RECALL_EDGE_VARIANT, recalls, batch=batch)
        run_batched(session, MERGE_RECALL_EDGE_MODEL_FALLBACK, recalls, batch=batch)
        # BOM 계층: System → Module → Part (참조 무결성 보장 순서).
        out["systems"]       = run_batched(session, MERGE_SYSTEM, systems, batch=batch)
        out["modules"]       = run_batched(session, MERGE_MODULE, modules, batch=batch)
        out["parts"]         = run_batched(session, MERGE_PART, parts, batch=batch)
        out["suppliers"]     = run_batched(session, MERGE_SUPPLIER, suppliers, batch=batch)

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
