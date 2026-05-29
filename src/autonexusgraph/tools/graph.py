"""Neo4j 그래프 탐색 도구 — 에이전트가 호출하는 사전 정의 함수.

자유 Cypher 금지(PRD §7.5.10). 모든 Cypher 는 ``tools/cypher_templates.py`` 의
레지스트리에서 가져오며, 함수는 파라미터만 채워 ``render_template`` + ``_run`` 호출.
이중 가드:
- 레지스트리: param 타입 / range / regex 검증 (TemplateError)
- ``_run``: cypher_guard READ-ONLY 정적 검사

설계 원칙:
- 읽기 전용 (Cypher 의 CREATE/MERGE/DELETE 안 씀 — 레지스트리에서 강제)
- 명시적 LIMIT — 그래프 폭발 방지 (DEFAULT_LIMIT=50, HARD_LIMIT=500)
- entity_resolution: corp_code 우선, name 은 보조
- snapshot_year/date 필터 옵션 — 시점별 답변 가능

API 시그니처는 이전 PR 과 동일 (에이전트 worker / 외부 호출자 호환).
"""

from __future__ import annotations

import logging
from typing import Any

from ..db.neo4j import get_driver
from .cypher_templates import TemplateError, render_template

log = logging.getLogger(__name__)


# 그래프 폭발 가드 — 어떤 함수도 이 한도를 넘기지 못함.
DEFAULT_LIMIT = 50
HARD_LIMIT = 500


def _run(cypher: str, **params: Any) -> list[dict]:
    """READ 단일 쿼리 실행 → list[dict] (record.data())."""
    from ..safety.cypher_guard import assert_read_only
    assert_read_only(cypher)
    driver = get_driver()
    with driver.session() as session:
        result = session.run(cypher, **params)
        return [dict(r) for r in result]


def _cap(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return DEFAULT_LIMIT
    return min(limit, HARD_LIMIT)


def _exec(template_name: str, **params: Any) -> list[dict]:
    """레지스트리 템플릿 → Cypher 렌더 → _run 호출.

    TemplateError 는 호출자에게 그대로 전파 (param 검증 실패).
    """
    cypher, bind = render_template(template_name, params)
    return _run(cypher, **bind)


# ── 회사 식별 ────────────────────────────────────────────────────────

def lookup_company_node(query: str, limit: int = 5) -> list[dict]:
    """이름·종목코드·corp_code 로 Neo4j :Company 노드 찾기.

    SQL 동명 함수 (``tools/financials.py:lookup_company``) 와 명명 충돌 방지를
    위해 ``_node`` 접미사. SQL 측은 master.companies 테이블 직접 조회.
    """
    return _exec("lookup_company", q=query.strip(), limit=_cap(limit))


# 하위호환 alias — 신규 코드는 lookup_company_node 사용 권장.
lookup_company = lookup_company_node


def lookup_person(name: str, birth_year: int | None = None,
                  limit: int = 5) -> list[dict]:
    """동명이인 안전 매칭. birth_year 없으면 (name, *) 모두 반환."""
    if birth_year is not None:
        return _exec("lookup_person_by_name_year",
                     name=name, by=birth_year, limit=_cap(limit))
    return _exec("lookup_person_by_name", name=name, limit=_cap(limit))


# ── 구조 그래프 탐색 ────────────────────────────────────────────────

def list_subsidiaries(parent_corp_code: str, *,
                      include_related: bool = False,
                      snapshot_year: int | None = None,
                      limit: int = DEFAULT_LIMIT) -> list[dict]:
    """모회사의 자회사. include_related=True 면 관계회사도."""
    tmpl = "list_subsidiaries_with_related" if include_related else "list_subsidiaries"
    return _exec(tmpl, cc=parent_corp_code, year=snapshot_year, limit=_cap(limit))


def list_parents(child_corp_code_or_name: str, *,
                 limit: int = DEFAULT_LIMIT) -> list[dict]:
    """이 회사가 자회사로 묶이는 모회사들."""
    return _exec("list_parents", k=child_corp_code_or_name, limit=_cap(limit))


def get_executives(corp_code: str, *,
                   role_contains: str | None = None,
                   snapshot_year: int | None = None,
                   limit: int = DEFAULT_LIMIT) -> list[dict]:
    """회사의 임원 목록 (role 은 r.role/r.duty 둘 다 substring 매칭)."""
    return _exec("get_executives",
                 cc=corp_code, role=role_contains,
                 year=snapshot_year, limit=_cap(limit))


def get_companies_of_person(name: str, birth_year: int | None = None, *,
                            role_contains: str | None = None,
                            limit: int = DEFAULT_LIMIT) -> list[dict]:
    """이 인물이 임원인 회사 목록."""
    if birth_year is not None:
        return _exec("get_companies_of_person_year",
                     name=name, by=birth_year,
                     role=role_contains, limit=_cap(limit))
    return _exec("get_companies_of_person",
                 name=name, role=role_contains, limit=_cap(limit))


def get_major_shareholders(corp_code: str, *,
                           min_pct: float = 0.0,
                           snapshot_year: int | None = None,
                           limit: int = DEFAULT_LIMIT) -> list[dict]:
    """회사의 최대주주 — 지분율 내림차순."""
    return _exec("get_major_shareholders",
                 cc=corp_code, min_pct=float(min_pct),
                 year=snapshot_year, limit=_cap(limit))


# ── 멀티홉 탐색 ──────────────────────────────────────────────────────

def find_paths(start_corp_code: str, end_corp_code: str,
               max_hops: int = 3) -> list[dict]:
    """두 회사 간 최단 경로. max_hops 1~5 만 허용."""
    hops = max(1, min(int(max_hops), 5))
    return _exec(f"find_paths_{hops}hops",
                 a=start_corp_code, b=end_corp_code)


def get_subgraph(corp_code: str, *,
                 depth: int = 1,
                 limit_nodes: int = 50) -> dict:
    """corp_code 중심 depth 이내 노드/엣지. APOC 우선, 미설치 시 fallback."""
    depth = max(1, min(int(depth), 3))
    limit = _cap(limit_nodes)
    try:
        rows = _exec(f"get_subgraph_d{depth}", cc=corp_code, limit=limit)
        if not rows:
            return {"nodes": [], "edges": []}
        rec = rows[0]
        nodes = [{
            "id": n.element_id, "labels": list(n.labels),
            "name": n.get("name") or n.get("corp_code"),
            "corp_code": n.get("corp_code"),
        } for n in rec["nodes"]]
        edges = [{
            "type": r.type,
            "start": r.start_node.element_id,
            "end": r.end_node.element_id,
            "props": dict(r),
        } for r in rec["relationships"]]
        return {"nodes": nodes, "edges": edges}
    except (TemplateError, Exception) as exc:
        # APOC 미설치 / 호출 실패 → 단순 폴백
        log.debug("APOC subgraph 실패 — fallback: %s", exc)
        rows = _exec("get_subgraph_fallback",
                     cc=corp_code, depth=depth, limit=limit)
        return {"raw": rows[0] if rows else {}}


# ── 뉴스 / 그룹 컨텍스트 ────────────────────────────────────────────

def list_mentioning_news(corp_code: str, *,
                         limit: int = DEFAULT_LIMIT) -> list[dict]:
    """뉴스 멘션 — 시점별로 정렬."""
    return _exec("list_mentioning_news", cc=corp_code, limit=_cap(limit))


def list_cooccurring(corp_code: str, *,
                     min_count: int = 2,
                     limit: int = DEFAULT_LIMIT) -> list[dict]:
    """뉴스 공동 언급 — 같은 기사에 함께 나온 회사들."""
    return _exec("list_cooccurring",
                 cc=corp_code, min=int(min_count), limit=_cap(limit))


def list_group_members(group_name: str, *,
                       limit: int = DEFAULT_LIMIT) -> list[dict]:
    """공정위 기업집단의 계열사 목록."""
    return _exec("list_group_members", g=group_name, limit=_cap(limit))


__all__ = [
    "lookup_company", "lookup_person",
    "list_subsidiaries", "list_parents",
    "get_executives", "get_companies_of_person",
    "get_major_shareholders",
    "find_paths", "get_subgraph",
    "list_mentioning_news", "list_cooccurring",
    "list_group_members",
]
