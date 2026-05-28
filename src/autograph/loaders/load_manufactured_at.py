"""(:VehicleModel)-[:MANUFACTURED_AT]->(:Plant) seed 적재.

source: ``ontology/auto/manufactured_at_seed.yaml`` — 한국 OEM 모델 + 글로벌 대표
모델 ~50건의 model↔plant 매핑. 모두 공개 IR / Wikipedia / 회사 홈페이지 기반,
PRD §3.5 B 등급 (deterministic + 공개 자료) → confidence 0.90 + validated.

선행 조건:
    1. ``make load-auto-seed-standards-plants`` — :Plant 노드 적재.
    2. ``make load-auto-neo4j`` — :VehicleModel 노드 적재 (NHTSA vPIC).

CLI:
    python -m autograph.loaders.load_manufactured_at
    python -m autograph.loaders.load_manufactured_at --dry-run

종료 코드:
    0: 정상
    seed 미적재면 row 0 으로 종료 (graceful).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
from dataclasses import dataclass, field

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.ingestion._common import normalize_corp_name

from ..ontology import load_manufactured_at_seed
from ._neo4j_helpers import edge_meta_cypher, run_batched


log = logging.getLogger(__name__)


_DEFAULT_CONFIDENCE = 0.90
_EXTRACTOR_NAME = "manufactured_at_seed"
_EXTRACTOR_VERSION = "v1"


@dataclass
class LoadStats:
    rows_seen:      int = 0
    edges_created:  int = 0
    plants_missing: int = 0
    models_missing: int = 0
    errors: list[str] = field(default_factory=list)


_MERGE_CYPHER = f"""
UNWIND $rows AS r
MATCH (m:VehicleModel)
WHERE toLower(replace(coalesce(m.name, ''), ' ', '')) = r.model_norm
MATCH (p:Plant {{code: r.plant_code}})
MERGE (m)-[edge:MANUFACTURED_AT]->(p)
SET {edge_meta_cypher('edge')},
    edge.valid_from = date(r.valid_from)
WITH edge
RETURN count(edge) AS n
"""


def _to_row(spec: dict) -> dict | None:
    model = (spec.get("model_name") or "").strip()
    plant = (spec.get("plant_code") or "").strip()
    if not model or not plant:
        return None
    valid_from = spec.get("valid_from")
    if isinstance(valid_from, _dt.date):
        vf = valid_from.isoformat()
    elif isinstance(valid_from, str) and valid_from:
        vf = valid_from
    else:
        vf = f"{_dt.date.today().year}-01-01"
    snapshot = int(vf[:4]) if vf else _dt.date.today().year
    norm = normalize_corp_name(model).replace(" ", "")
    return {
        "model_name":         model,
        "model_norm":         norm,
        "plant_code":         plant,
        "valid_from":         vf,
        "snapshot_year":      snapshot,
        "source_type":        "manual_seed",
        "source_id":          "ontology/auto/manufactured_at_seed.yaml",
        "confidence_score":   _DEFAULT_CONFIDENCE,
        "validated_status":   "validated",
        "extraction_method":  "manual",
    }


def load(*, dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    seed = load_manufactured_at_seed()
    if not seed:
        log.warning("[manufactured_at] seed 비어있음 — ontology/auto/manufactured_at_seed.yaml 확인")
        return stats

    rows: list[dict] = []
    for spec in seed:
        stats.rows_seen += 1
        r = _to_row(spec)
        if r:
            rows.append(r)

    if dry_run:
        log.info("[manufactured_at] DRY-RUN — would emit %d edges (seen=%d)",
                 len(rows), stats.rows_seen)
        for r in rows[:5]:
            log.info("  • %s -> %s (%s)", r["model_name"], r["plant_code"], r["valid_from"])
        return stats

    driver = get_driver()
    with driver.session() as session:
        # 누락 진단 — model 노드가 그래프에 있는지 (NHTSA vPIC 의 model name 표기 매칭).
        for r in rows:
            check = session.run(
                "MATCH (m:VehicleModel) "
                "WHERE toLower(replace(coalesce(m.name,''),' ','')) = $n "
                "RETURN count(m) AS n", n=r["model_norm"]
            ).single()
            if not check or int(check["n"]) == 0:
                stats.models_missing += 1
            chk = session.run(
                "MATCH (p:Plant {code:$c}) RETURN count(p) AS n", c=r["plant_code"]
            ).single()
            if not chk or int(chk["n"]) == 0:
                stats.plants_missing += 1

        n = run_batched(session, _MERGE_CYPHER, rows, batch=200)
        stats.edges_created = n

    log.info(
        "[manufactured_at] seen=%d edges=%d models_missing=%d plants_missing=%d",
        stats.rows_seen, stats.edges_created,
        stats.models_missing, stats.plants_missing,
    )
    if stats.models_missing:
        log.warning(
            "[manufactured_at] %d 매핑은 VehicleModel 노드 부재로 적재 안됨 — "
            "NHTSA vPIC 모델 이름 표기 차이 가능. load-auto-neo4j 적재 상태 확인.",
            stats.models_missing,
        )
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_manufactured_at")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load(dry_run=args.dry_run)


if __name__ == "__main__":
    main()


__all__ = ["load", "LoadStats"]
