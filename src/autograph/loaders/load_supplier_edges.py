"""(:Module|:Part)-[:SUPPLIED_BY]->(:Supplier) 결정적 적재.

본 PR 의 1차 source 는 ``ontology/auto/supplier_seed.yaml`` (manual seed, A grade).
Wikidata P176 SPARQL 자동 추출은 다음 PR 에서 — SPARQL 호출이 외부 의존이라 본 PR 의
DB-only 변경 범위를 넘어선다.

처리 흐름:
  1) seed yaml 의 각 supplier 를 auto.master_suppliers 에 UPSERT (없으면 신규 supplier_id)
  2) seed.components 의 각 row 마다 auto.components 에서 component_id 해결 (canonical_name + system_code)
  3) customer (OEM) 가 명시되면 그 OEM 의 VehicleModel 의 components 로 한정해 엣지
  4) Neo4j 에 (:Module|:Part)-[:SUPPLIED_BY]->(:Supplier) UNWIND 배치 적재

CLI:
    python -m autograph.loaders.load_supplier_edges
    python -m autograph.loaders.load_supplier_edges --dry-run
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name

from ..ontology import canonical_system_code
from ._neo4j_helpers import run_batched
from .load_bridge import _ensure_supplier


log = logging.getLogger(__name__)


_SEED_PATH = Path(__file__).resolve().parents[3] / "ontology" / "auto" / "supplier_seed.yaml"


@dataclass
class LoadStats:
    suppliers_upserted: int = 0
    edges_emitted:      int = 0
    component_unmatched: int = 0
    customer_unmatched:  int = 0
    errors: list[str]   = field(default_factory=list)


def _load_seed() -> list[dict]:
    if not _SEED_PATH.exists():
        log.warning("[supplier_edges] seed 없음: %s", _SEED_PATH)
        return []
    data = yaml.safe_load(_SEED_PATH.read_text(encoding="utf-8"))
    return data.get("suppliers") or []


def _resolve_component(cur, *, name: str, system_code: str) -> int | None:
    """canonical_name + system_code 로 auto.components 매칭. 없으면 None."""
    sys_code = canonical_system_code(system_code)
    cur.execute("""
        SELECT component_id FROM auto.components
         WHERE canonical_name = %s AND system_code = %s
         LIMIT 1
    """, (name, sys_code))
    r = cur.fetchone()
    if r:
        return r[0]
    # name_norm fallback
    cur.execute("""
        SELECT component_id FROM auto.components
         WHERE name_norm = %s AND system_code = %s
         LIMIT 1
    """, (normalize_corp_name(name), sys_code))
    r = cur.fetchone()
    return r[0] if r else None


def _ensure_component_module(cur, *, name: str, system_code: str) -> int:
    """component 가 없으면 manual 출처로 신규 등록 (level=4 Module). 매뉴얼 seed 가
    제공한 부품이 catalog 에 없는 케이스 — 명시적으로 등록해서 그래프에 노출.
    """
    sys_code = canonical_system_code(system_code)
    name_norm = normalize_corp_name(name)
    cur.execute("""
        INSERT INTO auto.components
          (canonical_name, name_norm, system_code, source,
           confidence, validated_status, level, snapshot_year)
        VALUES (%s, %s, %s, 'manual_supplier_seed', 1.000, 'verified',
                4, EXTRACT(YEAR FROM now())::SMALLINT)
        ON CONFLICT (canonical_name, system_code) DO UPDATE SET
          level = COALESCE(auto.components.level, 4)
        RETURNING component_id
    """, (name, name_norm, sys_code))
    return cur.fetchone()[0]


def _resolve_customer_models(cur, customer_name: str) -> list[int]:
    """OEM 이름 → 그 OEM 이 만드는 VehicleModel 의 model_id 들."""
    if not customer_name:
        return []
    cur.execute("""
        SELECT m.model_id
          FROM auto.master_vehicle_models m
          JOIN auto.master_manufacturers mm USING (manufacturer_id)
         WHERE mm.name_norm = %s
            OR mm.name_norm LIKE %s
    """, (normalize_corp_name(customer_name),
          normalize_corp_name(customer_name) + "%"))
    return [r[0] for r in cur.fetchall()]


# (:Module|:Part)-[:SUPPLIED_BY]->(:Supplier) UNWIND.
_MERGE_SUPPLIED_BY = """
UNWIND $rows AS r
MATCH (sup:Supplier {entity_id: r.supplier_entity_id})
MATCH (c) WHERE c.id = r.component_id AND (c:Module OR c:Part)
MERGE (c)-[rel:SUPPLIED_BY]->(sup)
SET   rel.source_type      = r.source_type,
      rel.source_id        = r.source_id,
      rel.extraction_method= r.extraction_method,
      rel.confidence_score = r.confidence_score,
      rel.validated_status = r.validated_status,
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year),
      rel.customer         = r.customer
"""


def load_supplier_edges(*, dry_run: bool = False, batch: int = 200) -> LoadStats:
    stats = LoadStats()
    seed = _load_seed()
    if not seed:
        log.info("[supplier_edges] seed 비어있음 — skip")
        return stats

    conn = get_connection()
    edges: list[dict] = []

    with conn.cursor() as cur:
        for s_row in seed:
            sname = s_row.get("supplier")
            if not sname:
                continue
            qid     = s_row.get("wikidata_qid")
            country = s_row.get("country")
            base_conf = float(s_row.get("confidence") or 0.95)

            # 1) supplier upsert → supplier_id 발급.
            try:
                supplier_id = _ensure_supplier(cur,
                    name=sname, wikidata_qid=qid, country=country,
                    lei=None, business_no=None,
                    source="manual_supplier_seed", source_ref="ontology/auto/supplier_seed.yaml",
                    confidence=base_conf)
                stats.suppliers_upserted += 1
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"supplier upsert {sname}: {e}")
                continue

            # 2) 각 component 엔트리 → 엣지 후보 생성.
            for comp in s_row.get("components") or []:
                if isinstance(comp, str):
                    name, sys_code, customer = comp, "UNKNOWN", None
                else:
                    name = comp.get("name")
                    sys_code = comp.get("system_code") or "UNKNOWN"
                    customer = comp.get("customer")
                if not name:
                    continue

                comp_id = _resolve_component(cur, name=name, system_code=sys_code)
                if comp_id is None:
                    # seed 가 도입한 새 component — Module(level=4) 로 등록.
                    comp_id = _ensure_component_module(cur, name=name, system_code=sys_code)

                # customer 가 명시되어도 SUPPLIED_BY 는 component→supplier 직접 엣지.
                # customer 는 메타로 보관 (한 supplier 가 여러 OEM 에 같은 component 공급할 때 행 구분).
                # 단, customer 가 명시됐는데 PG 에 그 OEM 이 없으면 seed 의 신뢰도가 떨어진다
                # 는 신호 — edge 를 candidate 로 다운그레이드 + confidence 감산.
                edge_conf = base_conf
                edge_status = "validated"
                if customer:
                    if not _resolve_customer_models(cur, customer):
                        stats.customer_unmatched += 1
                        edge_conf = round(base_conf * 0.85, 3)
                        edge_status = "candidate"

                edges.append({
                    "supplier_entity_id": str(supplier_id),
                    "component_id": comp_id,
                    "source_type": "manual_supplier_seed",
                    "source_id": f"yaml:{sname}",
                    "extraction_method": "manual",
                    "confidence_score": edge_conf,
                    "validated_status": edge_status,
                    "snapshot_year": None,
                    "customer": customer,
                })

    if dry_run:
        conn.rollback()
        log.info("[supplier_edges] DRY-RUN suppliers=%d would_emit=%d",
                 stats.suppliers_upserted, len(edges))
        return stats
    conn.commit()

    if edges:
        driver = get_driver()
        with driver.session() as session:
            stats.edges_emitted = run_batched(session, _MERGE_SUPPLIED_BY, edges, batch=batch)

    log.info("[supplier_edges] suppliers=%d edges=%d unmatched_customer=%d errors=%d",
             stats.suppliers_upserted, stats.edges_emitted,
             stats.customer_unmatched, len(stats.errors))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_supplier_edges")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_supplier_edges(dry_run=args.dry_run, batch=args.batch)


if __name__ == "__main__":
    main()


__all__ = ["load_supplier_edges", "LoadStats"]
