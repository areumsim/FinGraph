"""AutoGraph Neo4j tool — 차종/공급사 관계 탐색 (사전 정의 함수 풀).

자유 Cypher 금지. 모든 Cypher 는 ``autograph.cypher_templates_auto.AUTO_TEMPLATES``
레지스트리에서 가져오며, 함수는 파라미터만 채워 ``render_template`` + ``_run`` 호출.
이중 가드:
- 레지스트리: param 타입 / range / regex 검증
- ``_run``: cypher_guard READ-ONLY 정적 검사
"""

from __future__ import annotations

import logging
from typing import Any

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.tools.cypher_templates import render_template


log = logging.getLogger(__name__)


DEFAULT_LIMIT = 50
HARD_LIMIT = 500


def _cap(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return DEFAULT_LIMIT
    return min(int(limit), HARD_LIMIT)


def _run(cypher: str, **params: Any) -> list[dict]:
    """READ-only Neo4j 실행. cypher_guard 적용."""
    from autonexusgraph.safety.cypher_guard import assert_read_only
    assert_read_only(cypher)
    driver = get_driver()
    with driver.session() as session:
        result = session.run(cypher, **params)
        return [dict(r) for r in result]


def _exec(template_name: str, **params: Any) -> list[dict]:
    cypher, bind = render_template(template_name, params)
    return _run(cypher, **bind)


# ── 식별 ────────────────────────────────────────────────────
def lookup_vehicle(query: str, limit: int = 10) -> list[dict]:
    return _exec("auto_lookup_vehicle", q=(query or "").strip(), limit=_cap(limit))


def lookup_supplier(query: str, limit: int = 10) -> list[dict]:
    return _exec("auto_lookup_supplier", q=(query or "").strip(), limit=_cap(limit))


# ── 부품 ────────────────────────────────────────────────────
def list_components(*,
                    model_id: int | None = None,
                    variant_id: int | None = None,
                    system_code: str | None = None,
                    limit: int = DEFAULT_LIMIT) -> list[dict]:
    """차종 또는 트림이 보유한 컴포넌트. 둘 다 None 이면 빈 리스트."""
    if variant_id is not None:
        return _exec("auto_list_components_by_variant",
                     variant_id=int(variant_id),
                     system_code=system_code,
                     limit=_cap(limit))
    if model_id is not None:
        return _exec("auto_list_components_by_model",
                     model_id=int(model_id),
                     system_code=system_code,
                     limit=_cap(limit))
    return []


# ── 시스템 계층 (derived CONTAINS_SYSTEM) ───────────────────
def list_systems_of_model(model_id: int, *,
                          limit: int = DEFAULT_LIMIT) -> list[dict]:
    """차종이 보유한 시스템 (Level 3) 목록. derive_contains_system 가 채운 엣지."""
    return _exec("auto_systems_of_model",
                 model_id=int(model_id), limit=_cap(limit))


def list_models_with_system(system_code: str, *,
                            limit: int = DEFAULT_LIMIT) -> list[dict]:
    """시스템 코드로 해당 시스템을 보유한 차종들 역검색."""
    if not system_code:
        return []
    return _exec("auto_models_with_system",
                 system_code=str(system_code).strip(),
                 limit=_cap(limit))


# ── 리콜 ────────────────────────────────────────────────────
def list_recalls_affecting(*,
                           variant_id: int | None = None,
                           model_id: int | None = None,
                           year_min: int | None = None,
                           year_max: int | None = None,
                           limit: int = DEFAULT_LIMIT) -> list[dict]:
    if variant_id is not None:
        return _exec("auto_recalls_by_variant",
                     variant_id=int(variant_id),
                     year_min=year_min, year_max=year_max,
                     limit=_cap(limit))
    if model_id is not None:
        return _exec("auto_recalls_by_model",
                     model_id=int(model_id),
                     year_min=year_min, year_max=year_max,
                     limit=_cap(limit))
    return []


# ── 조사 (NHTSA ODI Investigations) ─────────────────────────
def list_investigations_affecting(*,
                                  variant_id: int | None = None,
                                  model_id: int | None = None,
                                  year_min: int | None = None,
                                  year_max: int | None = None,
                                  limit: int = DEFAULT_LIMIT) -> list[dict]:
    """차종/트림에 대한 NHTSA 결함 조사 (리콜 전 단계). recalls 의 자매."""
    if variant_id is not None:
        return _exec("auto_investigations_by_variant",
                     variant_id=int(variant_id),
                     year_min=year_min, year_max=year_max,
                     limit=_cap(limit))
    if model_id is not None:
        return _exec("auto_investigations_by_model",
                     model_id=int(model_id),
                     year_min=year_min, year_max=year_max,
                     limit=_cap(limit))
    return []


def get_investigation_recall_chain(investigation_id: int) -> list[dict]:
    """조사 → 후속 리콜 종결 (campno 가 매칭됐을 때만 비어있지 않음)."""
    return _exec("auto_investigation_recall_chain",
                 investigation_id=int(investigation_id))


# ── 공급사 ↔ 부품 ────────────────────────────────────────────
def get_suppliers_of_component(component_id: int, *,
                               limit: int = DEFAULT_LIMIT) -> list[dict]:
    return _exec("auto_suppliers_of_component",
                 component_id=int(component_id), limit=_cap(limit))


def get_vehicles_using_component(component_id: int, *,
                                 limit: int = DEFAULT_LIMIT) -> list[dict]:
    return _exec("auto_vehicles_using_component",
                 component_id=int(component_id), limit=_cap(limit))


# ── 경로 ────────────────────────────────────────────────────
def find_vehicle_component_paths(variant_id: int, component_id: int, *,
                                 max_hops: int = 4,
                                 limit: int = 20) -> list[dict]:
    """variant → component 최단경로 — Neo4j *1..N 동적 파라미터 불가 → hop 별 사전 등록."""
    hops = max(1, min(int(max_hops), 4))
    return _exec(f"auto_find_paths_{hops}hops",
                 a=int(variant_id), b=int(component_id))


__all__ = [
    "lookup_vehicle",
    "lookup_supplier",
    "list_components",
    "list_systems_of_model",
    "list_models_with_system",
    "list_recalls_affecting",
    "list_investigations_affecting",
    "get_investigation_recall_chain",
    "get_suppliers_of_component",
    "get_vehicles_using_component",
    "find_vehicle_component_paths",
]
