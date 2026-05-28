"""P3 자동차 프롬프트 schema-aware 검증.

LLM 호출 없이 prompt 구조 / json_schema enum / template 변수만 검증.
"""

from __future__ import annotations

from autograph.extractors.auto_relation_extractor import load_auto_prompt


def test_prompt_loads_with_required_sections():
    p = load_auto_prompt()
    assert "system" in p
    assert "user_template" in p
    assert "json_schema" in p
    assert "target_relations" in p


def test_target_relations_first_pass():
    p = load_auto_prompt()
    assert set(p["target_relations"]) == {"SUPPLIED_BY", "RECALL_OF"}


def test_json_schema_enums_match_prompt_target():
    p = load_auto_prompt()
    rel_enum = p["json_schema"]["schema"]["properties"]["relations"]["items"]["properties"]["relation"]["enum"]
    assert set(rel_enum) == {"SUPPLIED_BY", "RECALL_OF"}


def test_user_template_required_vars():
    """run_p3 가 채워주는 변수 목록과 일치해야."""
    p = load_auto_prompt()
    required_vars = ["manufacturer_id", "manufacturer_name", "model_id",
                     "model_name", "variant_id", "snapshot_year",
                     "source", "section", "chunk_id", "chunk_text"]
    tpl = p["user_template"]
    for v in required_vars:
        assert "{" + v + "}" in tpl, f"missing template var: {v}"


def test_entity_kind_enum_includes_module_and_part():
    p = load_auto_prompt()
    kinds = p["json_schema"]["schema"]["properties"]["entities"]["items"]["properties"]["kind"]["enum"]
    assert "Module" in kinds
    assert "Part"   in kinds
    assert "Supplier" in kinds
    assert "Recall"   in kinds
