"""AutoGraph 전용 Cypher 템플릿 — finance 의 ``TEMPLATES`` 와 동일 스키마.

자유 Cypher 금지. 본 모듈 export 인 ``AUTO_TEMPLATES`` 는 finance 의
``autonexusgraph.tools.cypher_templates.TEMPLATES`` 에 import 시점에 병합됨.

키 접두사: ``auto_*`` 로 finance 키와 충돌 회피.
모든 쿼리는 READ-ONLY (CREATE/MERGE/DELETE 금지).

라벨 매핑 (PRD §4.4):
  Level 0 Manufacturer / Level 1 VehicleModel / Level 2 VehicleVariant
  Level 3 System       / Level 4 Module       / Level 5 Part
  사이드: Recall, Complaint, Supplier, Plant, Standard
"""

from __future__ import annotations


# 자주 쓰는 LIMIT 범위 (0=무제한 비허용).
_INT_PK    = (int, ("range", 1, 9223372036854775000))
_LIMIT_500 = (int, ("range", 1, 500))
_YEAR      = (int, ("range", 1990, 2099))


AUTO_TEMPLATES: dict[str, dict] = {
    # ── 식별 ──────────────────────────────────────────────────
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
               v.fuel_type AS fuel_type,
               v.body_class AS body_class,
               v.drive_type AS drive_type
        ORDER BY mm.name, m.name, v.model_year DESC
        LIMIT $limit
        """,
        "required_params": ["q", "limit"],
        "param_schema": {"q": (str, None), "limit": _LIMIT_500},
    },

    "auto_lookup_supplier": {
        "cypher": """
        MATCH (s:Supplier)
        WHERE s.name = $q OR s.name CONTAINS $q OR s.name_norm CONTAINS $q
        RETURN s.entity_id    AS entity_id,
               s.name         AS name,
               s.wikidata_qid AS wikidata_qid,
               s.country      AS country,
               s.corp_code    AS corp_code,
               s.confidence_score AS confidence
        ORDER BY s.name
        LIMIT $limit
        """,
        "required_params": ["q", "limit"],
        "param_schema": {"q": (str, None), "limit": _LIMIT_500},
    },

    # ── BOM 계층 ──────────────────────────────────────────────
    # (VehicleModel)-[:CONTAINS_COMPONENT]->(Module|Part) — AI-Hub / LLM 추출 / 매뉴얼.
    # 키 이름은 tools.graph.list_components() 와 정렬 — 결과 라벨이 Module + Part 둘 다.
    "auto_list_components_by_model": {
        "cypher": """
        MATCH (m:VehicleModel {id: $model_id})-[rel:CONTAINS_COMPONENT]->(c)
        WHERE (c:Module OR c:Part)
          AND ($system_code IS NULL OR c.system_code = $system_code)
        RETURN c.id            AS component_id,
               labels(c)[0]    AS kind,
               c.name          AS name,
               c.system_code   AS system_code,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status,
               rel.source_type AS source_type,
               rel.snapshot_year AS snapshot_year
        ORDER BY rel.confidence_score DESC, c.system_code, c.name
        LIMIT $limit
        """,
        "required_params": ["model_id", "limit"],
        "param_schema": {
            "model_id": _INT_PK,
            "system_code": (str, None),
            "limit": _LIMIT_500,
        },
    },

    "auto_list_components_by_variant": {
        "cypher": """
        MATCH (v:VehicleVariant {id: $variant_id})<-[:HAS_VARIANT]-(m:VehicleModel)
        MATCH (m)-[rel:CONTAINS_COMPONENT]->(c)
        WHERE (c:Module OR c:Part)
          AND ($system_code IS NULL OR c.system_code = $system_code)
        RETURN c.id            AS component_id,
               labels(c)[0]    AS kind,
               c.name          AS name,
               c.system_code   AS system_code,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status,
               rel.snapshot_year AS snapshot_year
        ORDER BY rel.confidence_score DESC, c.system_code, c.name
        LIMIT $limit
        """,
        "required_params": ["variant_id", "limit"],
        "param_schema": {
            "variant_id": _INT_PK,
            "system_code": (str, None),
            "limit": _LIMIT_500,
        },
    },

    # (VehicleModel)-[:CONTAINS_SYSTEM]->(System) — derived by derive_contains_system.
    "auto_systems_of_model": {
        "cypher": """
        MATCH (m:VehicleModel {id: $model_id})-[rel:CONTAINS_SYSTEM]->(s:System)
        RETURN s.code AS system_code,
               s.name AS system_name,
               s.description AS description,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status,
               rel.snapshot_year    AS snapshot_year,
               rel.support_n        AS support_n
        ORDER BY rel.confidence_score DESC, s.code
        LIMIT $limit
        """,
        "required_params": ["model_id", "limit"],
        "param_schema": {"model_id": _INT_PK, "limit": _LIMIT_500},
    },

    "auto_models_with_system": {
        "cypher": """
        MATCH (s:System {code: $system_code})<-[rel:CONTAINS_SYSTEM]-(m:VehicleModel)
        OPTIONAL MATCH (mm:Manufacturer)-[:MANUFACTURES]->(m)
        RETURN m.id   AS model_id,
               m.name AS model_name,
               mm.id  AS manufacturer_id,
               mm.name AS mfr_name,
               rel.confidence_score AS confidence,
               rel.support_n        AS support_n
        ORDER BY rel.confidence_score DESC, mm.name, m.name
        LIMIT $limit
        """,
        "required_params": ["system_code", "limit"],
        "param_schema": {"system_code": (str, None), "limit": _LIMIT_500},
    },

    "auto_parts_in_module": {
        "cypher": """
        MATCH (p:Part)-[:CONTAINED_IN]->(m:Module {id: $module_id})
        RETURN p.id AS part_id, p.name AS name, p.system_code AS system_code
        ORDER BY p.name
        LIMIT $limit
        """,
        "required_params": ["module_id", "limit"],
        "param_schema": {"module_id": _INT_PK, "limit": _LIMIT_500},
    },

    # ── 리콜 ──────────────────────────────────────────────────
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
        ORDER BY rc.report_date DESC
        LIMIT $limit
        """,
        "required_params": ["variant_id", "limit"],
        "param_schema": {
            "variant_id": _INT_PK,
            "year_min": _YEAR, "year_max": _YEAR,
            "limit": _LIMIT_500,
        },
    },

    # variant 경로 + model 직결 경로를 UNION 해 중복 없이 합침. 이전의 OPTIONAL MATCH
    # collect+UNWIND 패턴이 r.confidence 차이로 row 가 갈리는 버그(0.11) 해소.
    "auto_recalls_by_model": {
        "cypher": """
        CALL {
          WITH $model_id AS mid
          MATCH (m:VehicleModel {id: mid})-[:HAS_VARIANT]->(v:VehicleVariant)
                -[rel:AFFECTED_BY]->(rc:Recall)
          RETURN rc, rel
          UNION
          WITH $model_id AS mid
          MATCH (m:VehicleModel {id: mid})-[rel:AFFECTED_BY]->(rc:Recall)
          RETURN rc, rel
        }
        WITH rc, rel
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
        ORDER BY rc.report_date DESC
        LIMIT $limit
        """,
        "required_params": ["model_id", "limit"],
        "param_schema": {
            "model_id": _INT_PK,
            "year_min": _YEAR, "year_max": _YEAR,
            "limit": _LIMIT_500,
        },
    },

    "auto_recalls_for_component": {        # Module 또는 Part 에 영향을 준 리콜
        "cypher": """
        MATCH (rc:Recall)-[rel:RECALL_OF]->(c)
        WHERE c.id = $component_id AND (c:Module OR c:Part)
        RETURN rc.id              AS recall_id,
               rc.source          AS source,
               rc.source_recall_no AS source_recall_no,
               rc.report_date     AS report_date,
               rc.component_text  AS component_text,
               rc.summary         AS summary,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status
        ORDER BY rc.report_date DESC
        LIMIT $limit
        """,
        "required_params": ["component_id", "limit"],
        "param_schema": {"component_id": _INT_PK, "limit": _LIMIT_500},
    },

    # ── 공급사 ↔ 부품 ────────────────────────────────────────
    "auto_suppliers_of_component": {
        "cypher": """
        MATCH (c)-[rel:SUPPLIED_BY]->(s:Supplier)
        WHERE c.id = $component_id AND (c:Module OR c:Part)
        RETURN s.entity_id    AS supplier_id,
               s.name         AS name,
               s.country      AS country,
               s.wikidata_qid AS wikidata_qid,
               s.corp_code    AS corp_code,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status,
               rel.snapshot_year    AS snapshot_year
        ORDER BY rel.confidence_score DESC, s.name
        LIMIT $limit
        """,
        "required_params": ["component_id", "limit"],
        "param_schema": {"component_id": _INT_PK, "limit": _LIMIT_500},
    },

    "auto_vehicles_using_component": {
        "cypher": """
        MATCH (c)<-[rel:CONTAINS_COMPONENT]-(m:VehicleModel)
        WHERE c.id = $component_id AND (c:Module OR c:Part)
        MATCH (mm:Manufacturer)-[:MANUFACTURES]->(m)
        RETURN m.id  AS model_id,
               m.name AS model_name,
               mm.id  AS manufacturer_id,
               mm.name AS mfr_name,
               rel.confidence_score AS confidence,
               rel.validated_status AS validated_status
        ORDER BY rel.confidence_score DESC, mm.name, m.name
        LIMIT $limit
        """,
        "required_params": ["component_id", "limit"],
        "param_schema": {"component_id": _INT_PK, "limit": _LIMIT_500},
    },

    "auto_vehicles_using_supplier": {       # Cross-Domain 진입점.
        "cypher": """
        MATCH (s:Supplier {entity_id: $entity_id})<-[:SUPPLIED_BY]-(c)
              <-[:CONTAINS_COMPONENT]-(m:VehicleModel)
              <-[:MANUFACTURES]-(mm:Manufacturer)
        WHERE (c:Module OR c:Part)
        RETURN DISTINCT
               mm.id   AS manufacturer_id,
               mm.name AS mfr_name,
               m.id    AS model_id,
               m.name  AS model_name,
               c.id    AS component_id,
               c.name  AS component_name
        ORDER BY mm.name, m.name
        LIMIT $limit
        """,
        "required_params": ["entity_id", "limit"],
        "param_schema": {"entity_id": (str, None), "limit": _LIMIT_500},
    },

    # ── 평가 / 규격 / 공장 ────────────────────────────────────
    "auto_safety_ratings": {
        "cypher": """
        MATCH (v:VehicleVariant {id: $variant_id})-[rel:SAFETY_RATED_BY]->(s:Standard)
        RETURN s.code AS standard_code,
               s.name AS standard_name,
               s.agency AS agency,
               rel.confidence_score AS confidence,
               rel.snapshot_year    AS snapshot_year
        ORDER BY s.code
        LIMIT $limit
        """,
        "required_params": ["variant_id", "limit"],
        "param_schema": {"variant_id": _INT_PK, "limit": _LIMIT_500},
    },

    "auto_plants_of_model": {
        "cypher": """
        MATCH (m:VehicleModel {id: $model_id})-[rel:MANUFACTURED_AT]->(p:Plant)
        RETURN p.code AS plant_code, p.name AS plant_name,
               p.country AS country, p.city AS city,
               rel.confidence_score AS confidence
        ORDER BY p.country, p.city
        LIMIT $limit
        """,
        "required_params": ["model_id", "limit"],
        "param_schema": {"model_id": _INT_PK, "limit": _LIMIT_500},
    },

    # ── 조사 (NHTSA ODI Investigations) ────────────────────
    # multi-variant 가 같은 inv 노드를 가리킬 때 (inv, rel) 페어 distinct 는 중복 살려둠
    # → inv 만 distinct 한 뒤 rel 의 메타는 첫 한 건만 추출 (B2 fix).
    "auto_investigations_by_variant": {
        "cypher": """
        MATCH (v:VehicleVariant {id: $variant_id})-[rel:INVESTIGATED_BY]->(inv:Investigation)
        WHERE ($year_min IS NULL OR inv.snapshot_year >= $year_min)
          AND ($year_max IS NULL OR inv.snapshot_year <= $year_max)
        WITH inv,
             head(collect(rel.confidence_score))  AS confidence,
             head(collect(rel.validated_status))  AS validated_status
        RETURN inv.id                 AS investigation_id,
               inv.action_number      AS action_number,
               inv.investigation_type AS investigation_type,
               inv.opened_date        AS opened_date,
               inv.closed_date        AS closed_date,
               inv.campno             AS campno,
               inv.subject            AS subject,
               inv.summary            AS summary,
               confidence             AS confidence,
               validated_status       AS validated_status
        ORDER BY inv.opened_date DESC
        LIMIT $limit
        """,
        "required_params": ["variant_id", "limit"],
        "param_schema": {
            "variant_id": _INT_PK,
            "year_min": _YEAR, "year_max": _YEAR,
            "limit": _LIMIT_500,
        },
    },

    "auto_investigations_by_model": {
        "cypher": """
        CALL {
          WITH $model_id AS mid
          MATCH (m:VehicleModel {id: mid})-[:HAS_VARIANT]->(:VehicleVariant)
                -[r:INVESTIGATED_BY]->(inv:Investigation)
          RETURN inv, r.confidence_score AS conf
          UNION
          WITH $model_id AS mid
          MATCH (m:VehicleModel {id: mid})-[r:INVESTIGATED_BY]->(inv:Investigation)
          RETURN inv, r.confidence_score AS conf
        }
        WITH inv, max(conf) AS confidence
        WHERE ($year_min IS NULL OR inv.snapshot_year >= $year_min)
          AND ($year_max IS NULL OR inv.snapshot_year <= $year_max)
        RETURN inv.id                 AS investigation_id,
               inv.action_number      AS action_number,
               inv.investigation_type AS investigation_type,
               inv.opened_date        AS opened_date,
               inv.closed_date        AS closed_date,
               inv.campno             AS campno,
               inv.subject            AS subject,
               inv.summary            AS summary,
               confidence             AS confidence
        ORDER BY inv.opened_date DESC
        LIMIT $limit
        """,
        "required_params": ["model_id", "limit"],
        "param_schema": {
            "model_id": _INT_PK,
            "year_min": _YEAR, "year_max": _YEAR,
            "limit": _LIMIT_500,
        },
    },

    # 조사 → 후속 리콜 추적 (campno 가 채워진 종결 조사만).
    "auto_investigation_recall_chain": {
        "cypher": """
        MATCH (inv:Investigation {id: $investigation_id})
        OPTIONAL MATCH (inv)-[:LED_TO_RECALL]->(rc:Recall)
        RETURN inv.action_number      AS action_number,
               inv.investigation_type AS investigation_type,
               inv.opened_date        AS opened_date,
               inv.closed_date        AS closed_date,
               rc.source_recall_no    AS recall_no,
               rc.summary             AS recall_summary,
               rc.report_date         AS recall_date
        """,
        "required_params": ["investigation_id"],
        "param_schema": {"investigation_id": _INT_PK},
    },

    "auto_complaints_by_variant": {
        "cypher": """
        MATCH (v:VehicleVariant {id: $variant_id})-[rel:REPORTED_IN]->(cmp:Complaint)
        WHERE ($year_min IS NULL OR cmp.snapshot_year >= $year_min)
          AND ($year_max IS NULL OR cmp.snapshot_year <= $year_max)
        RETURN cmp.id      AS complaint_id,
               cmp.source  AS source,
               cmp.source_complaint_no AS source_complaint_no,
               cmp.filed_date AS filed_date,
               cmp.summary    AS summary,
               rel.confidence_score AS confidence
        ORDER BY cmp.filed_date DESC
        LIMIT $limit
        """,
        "required_params": ["variant_id", "limit"],
        "param_schema": {
            "variant_id": _INT_PK,
            "year_min": _YEAR, "year_max": _YEAR,
            "limit": _LIMIT_500,
        },
    },

    # ── 경로 (variant ↔ module/part, 1~4 hop) ───────────────
    **{
        f"auto_find_paths_{h}hops": {
            "cypher": f"""
            MATCH p = shortestPath(
              (a:VehicleVariant {{id: $a}})-[*1..{h}]-(b)
            )
            WHERE (b:Module OR b:Part) AND b.id = $b
            RETURN [n IN nodes(p) | coalesce(n.name, toString(n.id))] AS node_path,
                   [r IN relationships(p) | type(r)] AS rel_types,
                   length(p) AS hops
            LIMIT 5
            """,
            "required_params": ["a", "b"],
            "param_schema": {"a": _INT_PK, "b": _INT_PK},
        }
        for h in range(1, 5)
    },
}


__all__ = ["AUTO_TEMPLATES"]
