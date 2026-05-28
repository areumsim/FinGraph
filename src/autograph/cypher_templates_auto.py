"""AutoGraph 전용 Cypher 템플릿 — finance 의 ``TEMPLATES`` 와 동일 스키마.

자유 Cypher 금지. 본 모듈 export 인 ``AUTO_TEMPLATES`` 는 finance 의
``autonexusgraph.tools.cypher_templates.TEMPLATES`` 에 import 시점에 병합됨.

키 접두사: ``auto_*`` 로 finance 키와 충돌 회피.
모든 쿼리는 READ-ONLY (CREATE/MERGE/DELETE 금지).
"""

from __future__ import annotations


AUTO_TEMPLATES: dict[str, dict] = {
    # ── 식별 ──
    "auto_lookup_vehicle": {
        "cypher": """
        MATCH (mm:Manufacturer)-[:MANUFACTURES]->(m:VehicleModel)
        OPTIONAL MATCH (m)-[:HAS_VARIANT]->(v:VehicleVariant)
        WHERE m.name = $q OR m.name CONTAINS $q
           OR mm.name = $q OR mm.name CONTAINS $q
        RETURN m.id AS model_id,
               m.name AS model_name,
               mm.id  AS manufacturer_id,
               mm.name AS mfr_name,
               v.id   AS variant_id,
               v.model_year AS model_year,
               v.trim AS trim,
               v.fuel_type AS fuel_type
        ORDER BY mm.name, m.name, v.model_year DESC
        LIMIT $limit
        """,
        "required_params": ["q", "limit"],
        "param_schema": {
            "q": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "auto_lookup_supplier": {
        "cypher": """
        MATCH (s:Supplier)
        WHERE s.name = $q OR s.name CONTAINS $q
        RETURN s.entity_id   AS entity_id,
               s.name        AS name,
               s.wikidata_qid AS wikidata_qid,
               s.country     AS country
        ORDER BY s.name
        LIMIT $limit
        """,
        "required_params": ["q", "limit"],
        "param_schema": {
            "q": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 부품 ──
    "auto_list_components_by_model": {
        "cypher": """
        MATCH (m:VehicleModel {id: $model_id})-[rel:CONTAINS_COMPONENT]->(c:Component)
        WHERE $system_code IS NULL OR c.system_code = $system_code
        RETURN c.id              AS component_id,
               c.name            AS name,
               c.system_code     AS system_code,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status,
               rel.source_type   AS source_type,
               rel.snapshot_year AS snapshot_year
        ORDER BY rel.confidence_score DESC, c.system_code, c.name
        LIMIT $limit
        """,
        "required_params": ["model_id", "limit"],
        "param_schema": {
            "model_id": (int, ("range", 1, 9223372036854775000)),
            "system_code": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "auto_list_components_by_variant": {
        "cypher": """
        MATCH (v:VehicleVariant {id: $variant_id})<-[:HAS_VARIANT]-(m:VehicleModel)
        MATCH (m)-[rel:CONTAINS_COMPONENT]->(c:Component)
        WHERE $system_code IS NULL OR c.system_code = $system_code
        RETURN c.id              AS component_id,
               c.name            AS name,
               c.system_code     AS system_code,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status,
               rel.snapshot_year AS snapshot_year
        ORDER BY rel.confidence_score DESC, c.system_code, c.name
        LIMIT $limit
        """,
        "required_params": ["variant_id", "limit"],
        "param_schema": {
            "variant_id": (int, ("range", 1, 9223372036854775000)),
            "system_code": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 리콜 ──
    "auto_recalls_by_variant": {
        "cypher": """
        MATCH (v:VehicleVariant {id: $variant_id})-[rel:AFFECTED_BY]->(rc:Recall)
        WHERE ($year_min IS NULL OR rc.snapshot_year >= $year_min)
          AND ($year_max IS NULL OR rc.snapshot_year <= $year_max)
        RETURN rc.id              AS recall_id,
               rc.source          AS source,
               rc.source_recall_no AS source_recall_no,
               rc.report_date     AS report_date,
               rc.component_text  AS component_text,
               rc.summary         AS summary,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status
        ORDER BY rc.report_date DESC NULLS LAST
        LIMIT $limit
        """,
        "required_params": ["variant_id", "limit"],
        "param_schema": {
            "variant_id": (int, ("range", 1, 9223372036854775000)),
            "year_min": (int, ("range", 1990, 2099)),
            "year_max": (int, ("range", 1990, 2099)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "auto_recalls_by_model": {
        "cypher": """
        MATCH (m:VehicleModel {id: $model_id})
        OPTIONAL MATCH (m)-[:HAS_VARIANT]->(v:VehicleVariant)-[r1:AFFECTED_BY]->(rc1:Recall)
        OPTIONAL MATCH (m)-[r2:AFFECTED_BY]->(rc2:Recall)
        WITH collect(DISTINCT {rc: rc1, rel: r1}) + collect(DISTINCT {rc: rc2, rel: r2}) AS rows
        UNWIND rows AS row
        WITH row WHERE row.rc IS NOT NULL
        WITH DISTINCT row.rc AS rc, row.rel AS rel
        WHERE ($year_min IS NULL OR rc.snapshot_year >= $year_min)
          AND ($year_max IS NULL OR rc.snapshot_year <= $year_max)
        RETURN rc.id              AS recall_id,
               rc.source          AS source,
               rc.source_recall_no AS source_recall_no,
               rc.report_date     AS report_date,
               rc.component_text  AS component_text,
               rc.summary         AS summary,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status
        ORDER BY rc.report_date DESC NULLS LAST
        LIMIT $limit
        """,
        "required_params": ["model_id", "limit"],
        "param_schema": {
            "model_id": (int, ("range", 1, 9223372036854775000)),
            "year_min": (int, ("range", 1990, 2099)),
            "year_max": (int, ("range", 1990, 2099)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 공급사 ↔ 부품 ──
    "auto_suppliers_of_component": {
        "cypher": """
        MATCH (c:Component {id: $component_id})-[rel:SUPPLIED_BY]->(s:Supplier)
        RETURN s.entity_id        AS supplier_id,
               s.name             AS name,
               s.country          AS country,
               s.wikidata_qid     AS wikidata_qid,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status,
               rel.snapshot_year  AS snapshot_year
        ORDER BY rel.confidence_score DESC, s.name
        LIMIT $limit
        """,
        "required_params": ["component_id", "limit"],
        "param_schema": {
            "component_id": (int, ("range", 1, 9223372036854775000)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "auto_vehicles_using_component": {
        "cypher": """
        MATCH (c:Component {id: $component_id})<-[rel:CONTAINS_COMPONENT]-(m:VehicleModel)
        MATCH (mm:Manufacturer)-[:MANUFACTURES]->(m)
        RETURN m.id   AS model_id,
               m.name AS model_name,
               mm.id  AS manufacturer_id,
               mm.name AS mfr_name,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status
        ORDER BY rel.confidence_score DESC, mm.name, m.name
        LIMIT $limit
        """,
        "required_params": ["component_id", "limit"],
        "param_schema": {
            "component_id": (int, ("range", 1, 9223372036854775000)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 경로 (variant → component, max hop 사전 등록) ──
    # 1~4 hop 만 허용.
    **{
        f"auto_find_paths_{h}hops": {
            "cypher": f"""
            MATCH p = shortestPath(
              (a:VehicleVariant {{id: $a}})-[*1..{h}]-(b:Component {{id: $b}})
            )
            RETURN [n IN nodes(p) | coalesce(n.name, toString(n.id))] AS node_path,
                   [r IN relationships(p) | type(r)] AS rel_types,
                   length(p) AS hops
            LIMIT 5
            """,
            "required_params": ["a", "b"],
            "param_schema": {
                "a": (int, ("range", 1, 9223372036854775000)),
                "b": (int, ("range", 1, 9223372036854775000)),
            },
        }
        for h in range(1, 5)
    },
}


__all__ = ["AUTO_TEMPLATES"]
