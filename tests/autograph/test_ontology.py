"""ontology/auto/*.yaml 의 SSOT 무결성 unit test.

- 모든 라벨이 entities.yaml 에서 키를 가지는가
- 모든 관계의 from/to 가 정의된 라벨인가
- 시스템 alias canonicalization 이 expected mapping 을 따르는가
- 엣지 의무 메타 키가 §6.7 와 일치하는가
"""

from __future__ import annotations

import pytest

from autograph.ontology import (
    canonical_system_code,
    entity_key_property,
    entity_labels,
    load_edge_required_meta,
    load_entities,
    load_relations,
    load_system_taxonomy,
    relation_endpoints,
    relation_types,
)


def test_all_labels_have_key():
    for label in entity_labels():
        assert entity_key_property(label), f"missing key for {label}"


def test_relation_endpoints_reference_known_labels():
    labels = set(entity_labels())
    for rt in relation_types():
        f, t = relation_endpoints(rt)
        assert f in labels, f"{rt}: from-label {f} not in entities.yaml"
        assert t in labels, f"{rt}: to-label {t} not in entities.yaml"


def test_edge_required_meta_matches_prd():
    """PRD §6.7 — confidence/source/snapshot/extraction 모두 필수."""
    meta = set(load_edge_required_meta())
    assert {"source_type", "source_id", "confidence_score",
            "validated_status", "snapshot_year", "extraction_method"} <= meta


@pytest.mark.parametrize("raw, expected", [
    ("powertrain", "POWERTRAIN"),
    ("ENGINE",     "POWERTRAIN"),
    ("BAT_PACK",   "BATTERY"),
    ("battery",    "BATTERY"),
    ("body",       "BODY"),
    ("electrical", "ELECTRICAL"),
    ("",           "UNKNOWN"),
    (None,         "UNKNOWN"),
    ("definitely_not_a_known_thing", "UNKNOWN"),
])
def test_canonical_system_code(raw, expected):
    assert canonical_system_code(raw) == expected


def test_required_core_labels_present():
    """PRD §4.4 의 필수 라벨이 모두 ontology 에 있어야."""
    have = set(entity_labels())
    must = {"Manufacturer", "VehicleModel", "VehicleVariant",
            "System", "Module", "Part", "Supplier", "Recall"}
    assert must <= have, f"missing: {must - have}"


def test_required_core_relations_present():
    have = set(relation_types())
    must = {"MANUFACTURES", "HAS_VARIANT", "AFFECTED_BY",
            "RECALL_OF", "SUPPLIED_BY", "CONTAINS_COMPONENT", "CONTAINED_IN"}
    assert must <= have, f"missing: {must - have}"
