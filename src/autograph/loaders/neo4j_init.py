"""AutoGraph 전용 Neo4j 제약/인덱스 일괄 생성.

다음 라벨에 대해 CONSTRAINT + INDEX:
    Manufacturer (id)
    VehicleModel (id)
    VehicleVariant (id)
    Component (id)
    Supplier (entity_id)        — bridge.corp_entity 의 entity_id 매핑
    Recall (id)
    System (code)
    Standard (code)
    Material (qid)
    Process (qid)

본 스크립트는 IF NOT EXISTS 동등 구문을 사용해 멱등.

CLI:
    python -m autograph.loaders.neo4j_init
"""

from __future__ import annotations

import argparse
import logging

from autonexusgraph.db.neo4j import get_driver


log = logging.getLogger(__name__)


CONSTRAINTS = [
    "CREATE CONSTRAINT auto_mfr_id_unique IF NOT EXISTS FOR (n:Manufacturer) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT auto_model_id_unique IF NOT EXISTS FOR (n:VehicleModel) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT auto_variant_id_unique IF NOT EXISTS FOR (n:VehicleVariant) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT auto_component_id_unique IF NOT EXISTS FOR (n:Component) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT auto_supplier_id_unique IF NOT EXISTS FOR (n:Supplier) REQUIRE n.entity_id IS UNIQUE",
    "CREATE CONSTRAINT auto_recall_id_unique IF NOT EXISTS FOR (n:Recall) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT auto_system_code_unique IF NOT EXISTS FOR (n:System) REQUIRE n.code IS UNIQUE",
]

INDEXES = [
    "CREATE INDEX auto_mfr_name IF NOT EXISTS FOR (n:Manufacturer) ON (n.name_norm)",
    "CREATE INDEX auto_model_name IF NOT EXISTS FOR (n:VehicleModel) ON (n.name_norm)",
    "CREATE INDEX auto_variant_year IF NOT EXISTS FOR (n:VehicleVariant) ON (n.model_year)",
    "CREATE INDEX auto_supplier_name IF NOT EXISTS FOR (n:Supplier) ON (n.name_norm)",
    "CREATE INDEX auto_recall_date IF NOT EXISTS FOR (n:Recall) ON (n.report_date)",
    "CREATE INDEX auto_component_sys IF NOT EXISTS FOR (n:Component) ON (n.system_code)",
]


def init_neo4j() -> None:
    driver = get_driver()
    with driver.session() as session:
        for stmt in CONSTRAINTS + INDEXES:
            try:
                session.run(stmt)
                log.info("[neo4j_init] %s", stmt.split()[1])
            except Exception as e:  # noqa: BLE001
                log.warning("[neo4j_init] skip %s — %s", stmt[:60], e)


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.neo4j_init")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_neo4j()
    log.info("[neo4j_init] done")


if __name__ == "__main__":
    main()
