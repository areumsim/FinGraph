"""AUTO_TEMPLATES 의 정합성 — required_params, label set, RO 보장."""

from __future__ import annotations

import re

from autograph.cypher_templates_auto import AUTO_TEMPLATES


def test_all_templates_read_only():
    """READ-ONLY — CREATE/MERGE/DELETE/SET 가 본문에 없어야."""
    forbidden = re.compile(r"\b(MERGE|CREATE|DELETE|SET|REMOVE)\b", re.I)
    for k, spec in AUTO_TEMPLATES.items():
        body = spec["cypher"]
        # WHERE 절 안의 SET / DELETE 절 등 — 단순 토큰 체크.
        m = forbidden.search(body)
        assert m is None, f"{k} contains write keyword: {m.group()}"


def test_required_params_match_schema():
    for k, spec in AUTO_TEMPLATES.items():
        req = set(spec.get("required_params", []))
        schema_keys = set(spec.get("param_schema", {}).keys())
        # required 는 schema 의 부분집합 (optional 도 schema 에 등장 가능).
        assert req <= schema_keys, f"{k}: required {req} not in schema keys {schema_keys}"


def test_supplier_query_uses_entity_id():
    """auto_lookup_supplier 는 :Supplier 의 자연 키 entity_id (neo4j_init 제약과 일치) 를 사용."""
    body = AUTO_TEMPLATES["auto_lookup_supplier"]["cypher"]
    assert "s.entity_id" in body


def test_component_queries_target_module_or_part():
    """component 류 쿼리는 :Module / :Part 라벨 union 으로 BOM 계층 둘 다 커버."""
    for k in ("auto_list_components_by_model",
              "auto_list_components_by_variant",
              "auto_suppliers_of_component",
              "auto_vehicles_using_component"):
        body = AUTO_TEMPLATES[k]["cypher"]
        assert "Module" in body or "Part" in body, f"{k} should target :Module/:Part"


def test_template_count_grew():
    """본 PR 에서 템플릿 수가 늘었는지 — 최소 18 개."""
    assert len(AUTO_TEMPLATES) >= 18
