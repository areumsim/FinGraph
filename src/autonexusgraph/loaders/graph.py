"""master.companies 기반 Neo4j 그래프 1차 적재.

생성/갱신되는 노드·관계 (ontology/entities.yaml + relations.yaml SSOT):

    (:Company {corp_code, name, stock_code, market_cap, sector_code})
    (:Market  {name})
    (:Industry {code})
    (:Person  {name})            -- CEO 한정 (임원 전체는 graph_structural.py)
    (:Company)-[:LISTED_IN]->(:Market)
    (:Company)-[:IN_INDUSTRY]->(:Industry)
    (:Company)-[:HAS_CEO]->(:Person)

설계 메모:
- 라벨은 ontology/entities.yaml 의 Industry / Market 와 일치 (이전 'Sector' 명칭은 폐기).
- 인덱스는 module-level constant 로 두고 idempotent 하게 매 적재마다 보장.
- 관계 MERGE 시점에 source 속성을 부여해 다중 출처 충돌 시 추적 가능.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import get_settings
from ._common import LoadStats


# 회사 + 시장 — UNWIND 배치, MERGE 기반 멱등.
CYPHER_COMPANIES = """
UNWIND $rows AS r
MERGE (c:Company {corp_code: r.corp_code})
SET c.name = r.name,
    c.stock_code = r.stock_code,
    c.market_cap = r.market_cap,
    c.sector_code = r.sector_code,
    c.updated_at = datetime()
WITH c, r
  WHERE r.market IS NOT NULL
MERGE (m:Market {name: r.market})
MERGE (c)-[rel:LISTED_IN]->(m)
SET rel.source = 'krx'
"""

# 산업 분류: DART induty_code / KSIC 코드를 키로. ontology 명칭은 Industry.
CYPHER_INDUSTRIES = """
UNWIND $rows AS r
MATCH (c:Company {corp_code: r.corp_code})
MERGE (s:Industry {code: r.sector_code})
MERGE (c)-[rel:IN_INDUSTRY]->(s)
SET rel.source = 'dart'
"""

# CEO: company.json 의 ceo_nm 콤마 분리 → Person 노드 + HAS_CEO.
# 임원 전체는 graph_structural.py 의 EXECUTIVE_OF 가 SSOT.
CYPHER_CEOS = """
UNWIND $rows AS r
MATCH (c:Company {corp_code: r.corp_code})
UNWIND r.ceos AS name
MERGE (p:Person {name: name})
ON CREATE SET p.source = 'dart_ceo'
MERGE (c)-[rel:HAS_CEO]->(p)
SET rel.source = 'dart'
"""

# 인덱스 — idempotent. 첫 적재 시 자동 생성.
CYPHER_INDEXES = [
    "CREATE INDEX company_corp_code IF NOT EXISTS FOR (c:Company)  ON (c.corp_code)",
    "CREATE INDEX company_name      IF NOT EXISTS FOR (c:Company)  ON (c.name)",
    "CREATE INDEX company_stock     IF NOT EXISTS FOR (c:Company)  ON (c.stock_code)",
    "CREATE INDEX market_name       IF NOT EXISTS FOR (m:Market)   ON (m.name)",
    "CREATE INDEX industry_code     IF NOT EXISTS FOR (s:Industry) ON (s.code)",
    "CREATE INDEX person_name       IF NOT EXISTS FOR (p:Person)   ON (p.name)",
    "CREATE INDEX person_name_birth IF NOT EXISTS FOR (p:Person)   ON (p.name, p.birth_year)",
    "CREATE INDEX newsevent_hash    IF NOT EXISTS FOR (n:NewsEvent) ON (n.article_hash)",
    "CREATE INDEX group_name        IF NOT EXISTS FOR (g:Group)    ON (g.name)",
]


def _parse_ceos(ceo_nm: str | None) -> list[str]:
    """company.json 의 ceo_nm 은 '전영현, 노태문' 같은 콤마/가운데점 구분 문자열."""
    if not ceo_nm:
        return []
    return [n.strip() for n in ceo_nm.replace("·", ",").split(",") if n.strip()]


def load_graph_companies(
    *,
    dry_run: bool = False,
    batch_size: int = 200,
) -> LoadStats:
    """master.companies → Neo4j (Company + Market + Industry + Person(CEO)).

    Args:
        dry_run: True 면 적재 없이 row 수만 계산.
        batch_size: UNWIND batch 크기. 200 은 295 회사 기준 2 batch 면 끝남.
    """
    stats = LoadStats()
    s = get_settings()

    import psycopg
    with psycopg.connect(s.postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT corp_code, corp_name, stock_code, market, sector, extra
            FROM master.companies
            WHERE is_active = TRUE
            ORDER BY corp_code
        """)
        all_rows = cur.fetchall()

    company_rows: list[dict[str, Any]] = []
    industry_rows: list[dict[str, Any]] = []
    ceo_rows: list[dict[str, Any]] = []

    for corp_code, name, stock_code, market, sector, extra in all_rows:
        # extra JSONB 는 psycopg2 에 따라 dict 또는 str 로 옴 — 양쪽 흡수.
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        elif extra is None:
            extra = {}

        market_cap = extra.get("market_cap_krw")
        company_rows.append({
            "corp_code": corp_code,
            "name": name,
            "stock_code": stock_code,
            "market_cap": market_cap,
            "market": market,
            "sector_code": sector,
        })
        if sector:
            industry_rows.append({"corp_code": corp_code, "sector_code": sector})
        ceos = _parse_ceos(extra.get("ceo_nm"))
        if ceos:
            ceo_rows.append({"corp_code": corp_code, "ceos": ceos})

    if dry_run:
        stats.inserted = len(company_rows)
        stats.batches = (len(company_rows) + batch_size - 1) // batch_size
        stats.sql_preview.append(CYPHER_COMPANIES.strip())
        stats.sql_preview.append(
            f"-- companies={len(company_rows)} industries={len(industry_rows)} ceos={len(ceo_rows)}"
        )
        return stats

    from ..db import neo4j as nx
    driver = nx.get_driver()

    def _batch(seq: list, n: int):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    with driver.session() as session:
        # 인덱스 먼저 (idempotent)
        for cypher in CYPHER_INDEXES:
            session.run(cypher)

        for batch in _batch(company_rows, batch_size):
            session.run(CYPHER_COMPANIES, rows=batch)
            stats.inserted += len(batch)
            stats.batches += 1

        for batch in _batch(industry_rows, batch_size):
            session.run(CYPHER_INDUSTRIES, rows=batch)
            stats.batches += 1

        for batch in _batch(ceo_rows, batch_size):
            session.run(CYPHER_CEOS, rows=batch)
            stats.batches += 1

    return stats
