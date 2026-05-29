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


def _prefetch_customer_models(cur, customer_names: set[str]) -> dict[str, list[int]]:
    """seed 내 모든 customer 이름의 model_id 매핑을 1회 round-trip 으로 prefetch.

    seed yaml 의 customers 수가 작더라도 SELECT 횟수가 component 마다 누적되어
    N+1 패턴 발생. 본 helper 가 ANY(prefixes) 로 한 번에 조회.
    """
    if not customer_names:
        return {}
    norm_pairs = [(name, normalize_corp_name(name)) for name in customer_names]
    norm_set = {n for _, n in norm_pairs if n}
    if not norm_set:
        return {n: [] for n in customer_names}

    cur.execute("""
        SELECT mm.name_norm, m.model_id
          FROM auto.master_vehicle_models m
          JOIN auto.master_manufacturers mm USING (manufacturer_id)
         WHERE mm.name_norm = ANY(%s)
            OR EXISTS (SELECT 1 FROM unnest(%s::text[]) AS prefix
                        WHERE mm.name_norm LIKE prefix || '%%')
    """, (list(norm_set), list(norm_set)))

    by_norm: dict[str, list[int]] = {n: [] for n in norm_set}
    for row in cur.fetchall():
        nn, mid = row[0], row[1]
        if nn in by_norm:
            by_norm[nn].append(mid)
        else:
            # prefix 매칭 — 어떤 customer 의 prefix 인지 역추적.
            for cn in norm_set:
                if nn.startswith(cn):
                    by_norm[cn].append(mid)
                    break

    return {name: by_norm.get(norm, []) for name, norm in norm_pairs}


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


# B1 fix — supplier_seed 가 PG 에 추가한 새 Module 들이 Neo4j 에 미반영이면
# 위 MATCH (c {id:r.component_id}) 가 0건 → SUPPLIED_BY 적재 실패.
# SUPPLIED_BY 이전에 component_id → :Module 노드 동기화 한 패스.
_MERGE_MODULE_FROM_PG = """
UNWIND $rows AS r
MERGE (c:Module {id: r.component_id})
ON CREATE SET c.name        = r.canonical_name,
              c.name_norm   = r.name_norm,
              c.system_code = r.system_code,
              c.source      = r.source,
              c.updated_at  = datetime()
ON MATCH SET  c.name        = coalesce(c.name, r.canonical_name),
              c.system_code = coalesce(c.system_code, r.system_code),
              c.updated_at  = datetime()
"""


def sync_modules_to_neo4j(conn, session, component_ids: list[int],
                          *, batch: int = 200) -> int:
    """B1 fix — PG components → Neo4j :Module 노드 동기화.

    edge MERGE 가 MATCH (c {id:...}) 에 의존하므로 사전 ensure 필수.
    """
    if not component_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute("""
            SELECT component_id, canonical_name, name_norm, system_code,
                   COALESCE(source, 'manual_supplier_seed') AS source
              FROM auto.components
             WHERE component_id = ANY(%s) AND level = 4
        """, (component_ids,))
        rows = [
            {"component_id": int(r[0]), "canonical_name": r[1],
             "name_norm": r[2], "system_code": r[3], "source": r[4]}
            for r in cur.fetchall()
        ]
    if not rows:
        return 0
    return run_batched(session, _MERGE_MODULE_FROM_PG, rows, batch=batch)


# B1 fix part 2 — supplier_seed 가 만든 auto.master_suppliers 의 19 supplier 가
# Neo4j 에 없음 (load_auto_neo4j.MERGE_SUPPLIER 는 bridge.corp_entity 의 wikidata
# supplier 만 적재). SUPPLIED_BY MATCH (sup:Supplier {entity_id:...}) 실패.
_MERGE_SUPPLIER_FROM_PG = """
UNWIND $rows AS r
MERGE (sup:Supplier {entity_id: r.entity_id})
ON CREATE SET sup.name      = r.name,
              sup.name_norm = r.name_norm,
              sup.country   = r.country,
              sup.wikidata_qid = r.wikidata_qid,
              sup.source    = r.source,
              sup.updated_at = datetime()
ON MATCH SET  sup.name      = coalesce(sup.name, r.name),
              sup.country   = coalesce(sup.country, r.country),
              sup.updated_at = datetime()
"""


def sync_suppliers_to_neo4j(conn, session, supplier_ids: list[int],
                            *, batch: int = 200) -> int:
    """PG master_suppliers → Neo4j :Supplier 동기화.

    entity_id = stringified supplier_id (bridge.corp_entity 컨벤션과 일치).
    """
    if not supplier_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute("""
            SELECT supplier_id, name, name_norm, country, wikidata_qid,
                   COALESCE(source, 'manual_supplier_seed') AS source
              FROM auto.master_suppliers
             WHERE supplier_id = ANY(%s)
        """, (supplier_ids,))
        rows = [
            {"entity_id": str(r[0]), "name": r[1], "name_norm": r[2],
             "country": r[3], "wikidata_qid": r[4], "source": r[5]}
            for r in cur.fetchall()
        ]
    if not rows:
        return 0
    return run_batched(session, _MERGE_SUPPLIER_FROM_PG, rows, batch=batch)


def load_supplier_edges(*, dry_run: bool = False, batch: int = 200) -> LoadStats:
    stats = LoadStats()
    seed = _load_seed()
    if not seed:
        log.info("[supplier_edges] seed 비어있음 — skip")
        return stats

    conn = get_connection()
    edges: list[dict] = []

    # 모든 seed entry 의 customer 이름 사전 추출 (N+1 회피).
    seed_customers: set[str] = set()
    for s_row in seed:
        for comp in s_row.get("components") or []:
            if isinstance(comp, dict):
                cust = comp.get("customer")
                if cust:
                    seed_customers.add(cust)

    with conn.cursor() as cur:
        customer_models = _prefetch_customer_models(cur, seed_customers)

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
                    if not customer_models.get(customer):
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
            # B1 fix — Module + Supplier 둘 다 사전 ensure. 둘 중 하나라도
            # Neo4j 에 없으면 SUPPLIED_BY MATCH 실패.
            comp_ids = sorted({int(e["component_id"]) for e in edges
                                if e.get("component_id")})
            sup_ids  = sorted({int(e["supplier_entity_id"]) for e in edges
                                if e.get("supplier_entity_id")
                                and str(e["supplier_entity_id"]).isdigit()})
            n_mod = sync_modules_to_neo4j(conn, session, comp_ids, batch=batch)
            n_sup = sync_suppliers_to_neo4j(conn, session, sup_ids, batch=batch)
            log.info("[supplier_edges] synced %d Module + %d Supplier nodes",
                     n_mod, n_sup)
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


__all__ = ["load_supplier_edges", "LoadStats",
           "sync_modules_to_neo4j", "sync_suppliers_to_neo4j"]
