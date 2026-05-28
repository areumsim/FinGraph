"""AutoGraph 사전 정의 tool — 자유 SQL/Cypher 금지.

모듈 구성 (finance 의 tools 패턴과 동일):
- spec     : PG 정형 (차종 식별, 제원, 안전 등급)
- graph    : Neo4j 관계 탐색 (리콜, 컴포넌트, 공급사) — cypher 템플릿 경유
- retrieve : pgvector 의미 검색 (자동차 청크 메타 필터)
- bridge   : Cross-Domain (corp_code ↔ entity_id)

본 패키지 import 시점에 AUTO_TEMPLATES 가 finance 의 TEMPLATES 에 병합된다 → 같은
render_template / _run / cypher_guard 파이프라인을 그대로 통과.
"""

# ── Cypher 템플릿 자동 병합 (import 1회) ─────────────────────
from autonexusgraph.tools.cypher_templates import TEMPLATES as _FIN_TEMPLATES
from ..cypher_templates_auto import AUTO_TEMPLATES as _AUTO_TEMPLATES

# finance 키와 충돌하면 자동 거부 (autograph 측 키는 'auto_' 접두사 규약).
for _k in _AUTO_TEMPLATES:
    if _k in _FIN_TEMPLATES:
        raise RuntimeError(
            f"AutoGraph cypher template key conflicts with finance: {_k!r}"
        )
_FIN_TEMPLATES.update(_AUTO_TEMPLATES)


from .spec import (
    compare_vehicles,
    get_safety_rating,
    get_spec,
    get_vehicle_info,
    lookup_vehicle,
)
from .graph import (
    find_vehicle_component_paths,
    get_suppliers_of_component,
    get_vehicles_using_component,
    list_components,
    list_recalls_affecting,
    lookup_supplier,
)
from .graph import lookup_vehicle as lookup_vehicle_graph
from .retrieve import (
    get_chunk_auto,
    search_by_metadata_auto,
    search_documents_auto,
)
from .bridge import (
    bridge_corp_to_entity,
    bridge_entity_to_corp,
    cross_query,
)

__all__ = [
    # spec
    "lookup_vehicle",
    "get_vehicle_info",
    "get_spec",
    "compare_vehicles",
    "get_safety_rating",
    # graph
    "lookup_vehicle_graph",
    "lookup_supplier",
    "list_components",
    "list_recalls_affecting",
    "get_suppliers_of_component",
    "get_vehicles_using_component",
    "find_vehicle_component_paths",
    # retrieve
    "search_documents_auto",
    "search_by_metadata_auto",
    "get_chunk_auto",
    # bridge
    "bridge_corp_to_entity",
    "bridge_entity_to_corp",
    "cross_query",
]
