"""AutoGraph 전용 Neo4j 제약/인덱스 일괄 생성.

라벨 목록은 ``ontology/auto/entities.yaml`` (SSOT) 에서 자동 로드.
key 컬럼은 ``entity_key_property(label)`` 가 알려주므로 라벨이 새로 추가되어도
본 파일을 수정하지 않아도 자동으로 CONSTRAINT 가 만들어진다.

추가로 흔히 쓰는 보조 인덱스 (name_norm / model_year / report_date / system_code …)
를 정의. 변경되면 모듈 상단 INDEXES 만 수정.

CLI:
    python -m autograph.loaders.neo4j_init
"""

from __future__ import annotations

import argparse
import logging

from autonexusgraph.db.neo4j import get_driver

from ..ontology import entity_key_property, entity_labels


log = logging.getLogger(__name__)


def _constraint(label: str, key: str) -> str:
    """label.key UNIQUE — Neo4j 5 구문."""
    cname = f"auto_{label.lower()}_{key}_unique"
    return (f"CREATE CONSTRAINT {cname} IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")


# 보조 인덱스 (검색용). 라벨이 ontology 에 있어야만 생성 시도.
_OPTIONAL_INDEXES: list[tuple[str, str, str]] = [
    # (label, property, index_name)
    ("Manufacturer",   "name_norm",   "auto_mfr_name"),
    ("VehicleModel",   "name_norm",   "auto_model_name"),
    ("VehicleVariant", "model_year",  "auto_variant_year"),
    ("VehicleVariant", "body_class",  "auto_variant_body"),
    ("Supplier",       "name_norm",   "auto_supplier_name"),
    ("Supplier",       "wikidata_qid","auto_supplier_qid"),
    ("Supplier",       "country",     "auto_supplier_country"),
    ("Recall",         "report_date", "auto_recall_date"),
    ("Recall",         "country",     "auto_recall_country"),
    ("Module",         "system_code", "auto_module_sys"),
    ("Module",         "name_norm",   "auto_module_name"),
    ("Part",           "system_code", "auto_part_sys"),
    ("Part",           "name_norm",   "auto_part_name"),
    ("System",         "name",        "auto_system_name"),
    ("Standard",       "agency",      "auto_standard_agency"),
    ("Plant",          "country",     "auto_plant_country"),
    ("Complaint",      "filed_date",  "auto_complaint_date"),
]


def init_neo4j() -> None:
    labels = entity_labels()
    constraints = [_constraint(label, entity_key_property(label)) for label in labels]
    label_set = set(labels)
    indexes = [
        f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
        for label, prop, name in _OPTIONAL_INDEXES
        if label in label_set
    ]

    driver = get_driver()
    with driver.session() as session:
        for stmt in constraints + indexes:
            try:
                session.run(stmt)
                log.info("[neo4j_init] %s", stmt.split()[1])
            except Exception as e:  # noqa: BLE001
                log.warning("[neo4j_init] skip %s — %s", stmt[:80], e)


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
