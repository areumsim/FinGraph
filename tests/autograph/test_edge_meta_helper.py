"""_neo4j_helpers: §6.7 의무 메타 SET 절 생성 검증."""

from __future__ import annotations

from autograph.loaders._neo4j_helpers import EDGE_META_KEYS, edge_meta_cypher


def test_edge_meta_keys_includes_required():
    assert "source_type"      in EDGE_META_KEYS
    assert "source_id"        in EDGE_META_KEYS
    assert "confidence_score" in EDGE_META_KEYS
    assert "validated_status" in EDGE_META_KEYS
    assert "snapshot_year"    in EDGE_META_KEYS
    assert "extraction_method" in EDGE_META_KEYS


def test_edge_meta_cypher_uses_var():
    body = edge_meta_cypher("rel")
    for key in EDGE_META_KEYS:
        assert f"rel.{key}" in body, f"{key} not set on rel"


def test_snapshot_year_has_fallback():
    body = edge_meta_cypher("r")
    # snapshot_year 만 NULL fallback (date().year) 적용.
    assert "coalesce(r.snapshot_year, date().year)" in body
