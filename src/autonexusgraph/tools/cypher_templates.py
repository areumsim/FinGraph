"""Cypher 템플릿 중앙 레지스트리 — PRD §7.5.9.

자유 Cypher 생성 금지. 도구 함수가 호출할 모든 Cypher 는 본 레지스트리에 등록되어
``render_template(name, params)`` 로 검증 + 바인드된다.

각 템플릿 항목:
- ``cypher``: Neo4j Cypher 문자열. ``$param`` 형태의 바인드 파라미터만 사용
- ``required_params``: 필수 파라미터 이름 (없으면 검증 실패)
- ``param_schema``: ``{name: (type, optional_constraint)}`` 형식. constraint 는:
  - ``None``      — type 만 검증
  - ``("enum", set[str])``  — 값이 enum 에 포함돼야
  - ``("range", min, max)`` — 숫자 범위
  - ``("regex", pattern)``  — 정규식 매칭

동적 cypher (relation 종류, hops 개수 등) 가 필요한 경우 enum/range 검증된 값으로
``cypher`` 문자열을 안전하게 빌드한 별도 템플릿으로 등록한다. 외부에서 cypher 문자열
포매팅 금지.

``render_template`` 후 결과는 그대로 ``_run(cypher, **params)`` 에 넘기면 된다.
``_run`` 이 추가로 cypher_guard (READ-ONLY 검사) 통과시킨다.
"""

from __future__ import annotations

import re
from typing import Any


# ── 검증 타입 헬퍼 ──────────────────────────────────────────
class TemplateError(ValueError):
    """템플릿 검증 실패."""


_BasicType = type   # int / str / float / bool


# ── 템플릿 레지스트리 ──────────────────────────────────────
TEMPLATES: dict[str, dict] = {
    # ── 회사 식별 ──
    "lookup_company": {
        "cypher": """
        MATCH (c:Company)
        WHERE c.corp_code = $q
           OR c.stock_code = $q
           OR c.name = $q
           OR c.name CONTAINS $q
        RETURN c.corp_code AS corp_code,
               c.name      AS name,
               c.stock_code AS stock_code,
               c.wikidata_qid AS wikidata_qid,
               c.wikipedia_title_ko AS wikipedia_title_ko
        LIMIT $limit
        """,
        "required_params": ["q", "limit"],
        "param_schema": {
            "q": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 인물 ──
    "lookup_person_by_name": {
        "cypher": """
        MATCH (p:Person {name: $name})
        OPTIONAL MATCH (p)-[:EXECUTIVE_OF]->(c:Company)
        WITH p, collect(DISTINCT c.name)[..5] AS sample_corps
        RETURN p.name AS name, p.birth_year AS birth_year,
               p.gender AS gender, sample_corps
        ORDER BY p.birth_year DESC
        LIMIT $limit
        """,
        "required_params": ["name", "limit"],
        "param_schema": {
            "name": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "lookup_person_by_name_year": {
        "cypher": """
        MATCH (p:Person {name: $name, birth_year: $by})
        OPTIONAL MATCH (p)-[:EXECUTIVE_OF]->(c:Company)
        WITH p, collect(DISTINCT c.name)[..5] AS sample_corps
        RETURN p.name AS name, p.birth_year AS birth_year,
               p.gender AS gender, sample_corps
        LIMIT $limit
        """,
        "required_params": ["name", "by", "limit"],
        "param_schema": {
            "name": (str, None),
            "by": (int, ("range", 1900, 2050)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 자회사·모회사 ──
    "list_subsidiaries": {
        "cypher": """
        MATCH (child:Company)-[r:SUBSIDIARY_OF]->(parent:Company {corp_code: $cc})
        WHERE $year IS NULL OR r.rcept_year = $year
        RETURN child.corp_code AS child_corp_code,
               child.name      AS child_name,
               type(r)         AS relation,
               r.ownership_pct AS ownership_pct,
               r.snapshot_date AS snapshot_date
        ORDER BY r.ownership_pct DESC
        LIMIT $limit
        """,
        "required_params": ["cc", "limit"],
        "param_schema": {
            "cc": (str, ("regex", r"^\d{8}$")),
            "year": (int, ("range", 1990, 2099)),   # optional
            "limit": (int, ("range", 1, 500)),
        },
    },

    "list_subsidiaries_with_related": {
        "cypher": """
        MATCH (child:Company)-[r:SUBSIDIARY_OF|RELATED_TO]->(parent:Company {corp_code: $cc})
        WHERE $year IS NULL OR r.rcept_year = $year
        RETURN child.corp_code AS child_corp_code,
               child.name      AS child_name,
               type(r)         AS relation,
               r.ownership_pct AS ownership_pct,
               r.snapshot_date AS snapshot_date
        ORDER BY r.ownership_pct DESC
        LIMIT $limit
        """,
        "required_params": ["cc", "limit"],
        "param_schema": {
            "cc": (str, ("regex", r"^\d{8}$")),
            "year": (int, ("range", 1990, 2099)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "list_parents": {
        "cypher": """
        MATCH (child:Company)-[r:SUBSIDIARY_OF]->(parent:Company)
        WHERE child.corp_code = $k OR child.name = $k
        RETURN parent.corp_code AS parent_corp_code,
               parent.name      AS parent_name,
               r.ownership_pct  AS ownership_pct,
               r.snapshot_date  AS snapshot_date
        ORDER BY r.snapshot_date DESC
        LIMIT $limit
        """,
        "required_params": ["k", "limit"],
        "param_schema": {
            "k": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 임원 ──
    "get_executives": {
        "cypher": """
        MATCH (p:Person)-[r:EXECUTIVE_OF]->(c:Company {corp_code: $cc})
        WHERE ($role IS NULL
               OR r.role CONTAINS $role
               OR (r.duty IS NOT NULL AND r.duty CONTAINS $role))
          AND ($year IS NULL OR r.snapshot_year = $year)
        RETURN p.name        AS name,
               p.birth_year  AS birth_year,
               r.role        AS role,
               r.registered  AS registered,
               r.full_time   AS full_time,
               r.duty        AS duty,
               r.snapshot_year AS snapshot_year
        ORDER BY r.snapshot_year DESC, p.name
        LIMIT $limit
        """,
        "required_params": ["cc", "limit"],
        "param_schema": {
            "cc": (str, ("regex", r"^\d{8}$")),
            "role": (str, None),
            "year": (int, ("range", 1990, 2099)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "get_companies_of_person": {
        "cypher": """
        MATCH (p:Person {name: $name})-[r:EXECUTIVE_OF]->(c:Company)
        WHERE $role IS NULL OR r.role CONTAINS $role
        RETURN c.corp_code AS corp_code,
               c.name      AS company_name,
               r.role      AS role,
               r.snapshot_year AS snapshot_year
        ORDER BY r.snapshot_year DESC
        LIMIT $limit
        """,
        "required_params": ["name", "limit"],
        "param_schema": {
            "name": (str, None),
            "role": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "get_companies_of_person_year": {
        "cypher": """
        MATCH (p:Person {name: $name, birth_year: $by})-[r:EXECUTIVE_OF]->(c:Company)
        WHERE $role IS NULL OR r.role CONTAINS $role
        RETURN c.corp_code AS corp_code,
               c.name      AS company_name,
               r.role      AS role,
               r.snapshot_year AS snapshot_year
        ORDER BY r.snapshot_year DESC
        LIMIT $limit
        """,
        "required_params": ["name", "by", "limit"],
        "param_schema": {
            "name": (str, None),
            "by": (int, ("range", 1900, 2050)),
            "role": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 최대주주 ──
    "get_major_shareholders": {
        "cypher": """
        MATCH (h)-[r:MAJOR_SHAREHOLDER_OF]->(c:Company {corp_code: $cc})
        WHERE r.ownership_pct >= $min_pct
          AND ($year IS NULL OR r.snapshot_year = $year)
        RETURN labels(h)[0]   AS holder_kind,
               h.name         AS holder_name,
               h.corp_code    AS holder_corp_code,
               r.ownership_pct AS ownership_pct,
               r.relation     AS relation,
               r.snapshot_year AS snapshot_year
        ORDER BY r.ownership_pct DESC
        LIMIT $limit
        """,
        "required_params": ["cc", "min_pct", "limit"],
        "param_schema": {
            "cc": (str, ("regex", r"^\d{8}$")),
            "min_pct": (float, ("range", 0.0, 100.0)),
            "year": (int, ("range", 1990, 2099)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 멀티홉 ──
    # find_paths 의 hops 는 cypher 문자열에 박혀야 하는데, Neo4j 5.x 는
    # ``*1..N`` 의 N 을 파라미터로 못 받기 때문에 hops 별 사전 등록.
    # 1~5 hops 만 허용.
    **{
        f"find_paths_{h}hops": {
            "cypher": f"""
            MATCH p = shortestPath(
              (a:Company {{corp_code: $a}})-[*1..{h}]-(b:Company {{corp_code: $b}})
            )
            RETURN [n IN nodes(p) | coalesce(n.name, n.corp_code)] AS node_path,
                   [r IN relationships(p) | type(r)] AS rel_types,
                   length(p) AS hops
            LIMIT 5
            """,
            "required_params": ["a", "b"],
            "param_schema": {
                "a": (str, ("regex", r"^\d{8}$")),
                "b": (str, ("regex", r"^\d{8}$")),
            },
        }
        for h in range(1, 6)
    },

    # ── 서브그래프 (APOC) ──
    **{
        f"get_subgraph_d{d}": {
            "cypher": f"""
            MATCH (center:Company {{corp_code: $cc}})
            CALL apoc.path.subgraphAll(center, {{maxLevel: {d}, limit: $limit}})
            YIELD nodes, relationships
            RETURN nodes, relationships
            """,
            "required_params": ["cc", "limit"],
            "param_schema": {
                "cc": (str, ("regex", r"^\d{8}$")),
                "limit": (int, ("range", 1, 500)),
            },
        }
        for d in range(1, 4)
    },

    "get_subgraph_fallback": {
        "cypher": """
        MATCH (center:Company {corp_code: $cc})
        OPTIONAL MATCH (center)-[r1]-(n1)
        OPTIONAL MATCH (n1)-[r2]-(n2)
          WHERE n2 <> center AND $depth >= 2
        RETURN center, collect(DISTINCT n1)[..$limit] AS depth1,
               collect(DISTINCT n2)[..$limit] AS depth2,
               collect(DISTINCT r1) AS rels1,
               collect(DISTINCT r2) AS rels2
        """,
        "required_params": ["cc", "depth", "limit"],
        "param_schema": {
            "cc": (str, ("regex", r"^\d{8}$")),
            "depth": (int, ("range", 1, 3)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    # ── 뉴스 / 그룹 ──
    "list_mentioning_news": {
        "cypher": """
        MATCH (n:NewsEvent)-[m:MENTIONS]->(c:Company {corp_code: $cc})
        RETURN n.article_hash AS article_hash,
               n.title        AS title,
               n.source       AS source,
               n.published_at AS published_at,
               n.url          AS url,
               m.confidence   AS confidence
        ORDER BY n.published_at DESC
        LIMIT $limit
        """,
        "required_params": ["cc", "limit"],
        "param_schema": {
            "cc": (str, ("regex", r"^\d{8}$")),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "list_cooccurring": {
        "cypher": """
        MATCH (a:Company {corp_code: $cc})-[r:CO_MENTIONED_WITH]-(b:Company)
        WHERE r.count >= $min
        RETURN b.corp_code AS corp_code,
               b.name      AS name,
               r.count     AS co_count,
               r.last_seen AS last_seen
        ORDER BY r.count DESC
        LIMIT $limit
        """,
        "required_params": ["cc", "min", "limit"],
        "param_schema": {
            "cc": (str, ("regex", r"^\d{8}$")),
            "min": (int, ("range", 1, 10000)),
            "limit": (int, ("range", 1, 500)),
        },
    },

    "list_group_members": {
        "cypher": """
        MATCH (c:Company)-[:BELONGS_TO_GROUP]->(g:Group {name: $g})
        RETURN c.corp_code AS corp_code, c.name AS name
        ORDER BY c.name
        LIMIT $limit
        """,
        "required_params": ["g", "limit"],
        "param_schema": {
            "g": (str, None),
            "limit": (int, ("range", 1, 500)),
        },
    },
}


# ── 검증 + 렌더 ─────────────────────────────────────────────
def _validate_param(name: str, value: Any, schema: tuple) -> None:
    """단일 파라미터 type / constraint 검증."""
    expected_type, constraint = schema if isinstance(schema, tuple) else (schema, None)
    # None 은 nullable param 으로 허용 (Cypher 안에서 IS NULL 체크 가능)
    if value is None:
        return
    # bool 은 int 의 subclass — int/float 자리에 들어오면 거절 (의도치 않은 통과 방지)
    if isinstance(value, bool) and expected_type in (int, float):
        raise TemplateError(f"{name}: bool 은 {expected_type.__name__} 으로 허용 안 함")
    if not isinstance(value, expected_type):
        # float 자리에 int 만 허용
        if expected_type is float and isinstance(value, int):
            pass
        else:
            raise TemplateError(
                f"{name}: 기대 type={expected_type.__name__}, 실제 {type(value).__name__}={value!r}"
            )
    if constraint is None:
        return
    kind = constraint[0]
    if kind == "enum":
        allowed = constraint[1]
        if value not in allowed:
            raise TemplateError(f"{name}: 허용값 {allowed}, 실제 {value!r}")
    elif kind == "range":
        lo, hi = constraint[1], constraint[2]
        if not (lo <= value <= hi):
            raise TemplateError(f"{name}: 범위 [{lo}, {hi}], 실제 {value!r}")
    elif kind == "regex":
        pat = constraint[1]
        if not re.fullmatch(pat, str(value)):
            raise TemplateError(f"{name}: 정규식 {pat!r} 미매칭, 실제 {value!r}")
    else:
        raise TemplateError(f"{name}: 알 수 없는 constraint kind={kind!r}")


def render_template(name: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """이름으로 템플릿 조회 → 검증된 파라미터와 함께 (cypher, bind_params) 반환.

    - 미등록 이름 → TemplateError
    - 필수 파라미터 누락 → TemplateError
    - 타입 / range / regex 위반 → TemplateError
    - 정의되지 않은 추가 파라미터 → 무시 (호환성). 다만 cypher 안에 사용되지 않으면
      _run 의 Neo4j 가 그냥 무시.
    """
    if name not in TEMPLATES:
        raise TemplateError(f"unknown template: {name}")
    spec = TEMPLATES[name]
    required = spec.get("required_params") or []
    schema = spec.get("param_schema") or {}
    p = dict(params)

    # 필수
    missing = [k for k in required if k not in p]
    if missing:
        raise TemplateError(f"{name}: 필수 파라미터 누락 {missing}")

    # 타입·constraint 검증 — schema 에 있는 키만
    for k, sch in schema.items():
        if k in p:
            _validate_param(k, p[k], sch)

    return spec["cypher"], p


def list_templates() -> list[str]:
    """등록된 템플릿 이름 목록 (디버그용)."""
    return sorted(TEMPLATES.keys())


__all__ = ["TEMPLATES", "TemplateError", "render_template", "list_templates"]
