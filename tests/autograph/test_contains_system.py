"""derive_contains_system + auto_systems_of_model 템플릿 단위 검증.

DB 없이 — 모듈 import / cypher 템플릿 등록 / param 검증만.
"""

from __future__ import annotations

import pytest


def test_auto_systems_of_model_template_registered():
    import autograph.tools  # noqa: F401 — side-effect 로 TEMPLATES 병합
    from autonexusgraph.tools.cypher_templates import TEMPLATES, render_template

    assert "auto_systems_of_model" in TEMPLATES
    assert "auto_models_with_system" in TEMPLATES

    cypher, bind = render_template("auto_systems_of_model",
                                    {"model_id": 1, "limit": 10})
    assert "CONTAINS_SYSTEM" in cypher
    assert bind["model_id"] == 1


def test_auto_systems_of_model_param_validation():
    import autograph.tools  # noqa: F401
    from autonexusgraph.tools.cypher_templates import TemplateError, render_template

    with pytest.raises(TemplateError):
        render_template("auto_systems_of_model",
                        {"model_id": 1, "limit": 9999})
    with pytest.raises(TemplateError):
        render_template("auto_models_with_system",
                        {"system_code": "POWERTRAIN"})  # limit 누락


def test_derive_contains_system_importable():
    from autograph.loaders import derive_contains_system as d
    assert hasattr(d, "derive_contains_system")
    assert hasattr(d, "DeriveStats")
    # cypher 본문 RO 검증 — MERGE/SET 은 합법이지만 DELETE/CREATE 노드는 없어야.
    body = d._DERIVE_CYPHER
    assert "MERGE (m)-[rel:CONTAINS_SYSTEM]" in body
    assert "DELETE" not in body
    assert "CREATE (" not in body, "노드 신규 생성 금지 — MERGE rel 만"


def test_list_systems_of_model_function_exists():
    from autograph.tools import graph as g
    assert callable(g.list_systems_of_model)
    assert callable(g.list_models_with_system)


def test_contains_system_in_auto_graph_whitelist():
    """workers 의 _AUTO_GRAPH_ALLOWED 에 새 intent 등록 확인."""
    from autonexusgraph.agents.workers import _AUTO_GRAPH_ALLOWED
    assert "list_systems_of_model" in _AUTO_GRAPH_ALLOWED
    assert "list_models_with_system" in _AUTO_GRAPH_ALLOWED
