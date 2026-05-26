"""Neo4j 그래프 로더 — Company / Market / Sector / Person 노드 + 관계.

PRD §4.3 — Neo4j 는 관계 중심 저장소. 정량 수치는 PG.
초기 노드/관계 (Phase 3-3):
    (:Company {corp_code, name, stock_code, market_cap})
    (:Market {name})
    (:Sector {code})
    (:Person {name})            -- CEO 만 (임원/이사진은 후속)
    (:Company)-[:LISTED_IN]->(:Market)
    (:Company)-[:IN_SECTOR]->(:Sector)
    (:Company)-[:HAS_CEO]->(:Person)

후속 (Phase 4+):
    (:Company)-[:SUBSIDIARY_OF]->(:Company)
    (:Person)-[:EXECUTIVE_OF {role, since}]->(:Company)
    (:Company)-[:PARTNER_OF]->(:Company)
"""

from __future__ import annotations

import json
from typing import Any

from ..config import get_settings
from ._common import LoadStats


# Cypher — UNWIND 배치 idempotent
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
MERGE (c)-[:LISTED_IN]->(m)
"""

CYPHER_SECTORS = """
UNWIND $rows AS r
MATCH (c:Company {corp_code: r.corp_code})
MERGE (s:Sector {code: r.sector_code})
MERGE (c)-[:IN_SECTOR]->(s)
"""

CYPHER_CEOS = """
UNWIND $rows AS r
MATCH (c:Company {corp_code: r.corp_code})
UNWIND r.ceos AS name
MERGE (p:Person {name: name})
MERGE (c)-[:HAS_CEO]->(p)
"""

# 인덱스 (idempotent)
CYPHER_INDEXES = [
    "CREATE INDEX company_corp_code IF NOT EXISTS FOR (c:Company) ON (c.corp_code)",
    "CREATE INDEX company_name      IF NOT EXISTS FOR (c:Company) ON (c.name)",
    "CREATE INDEX company_stock     IF NOT EXISTS FOR (c:Company) ON (c.stock_code)",
    "CREATE INDEX market_name       IF NOT EXISTS FOR (m:Market)  ON (m.name)",
    "CREATE INDEX sector_code       IF NOT EXISTS FOR (s:Sector)  ON (s.code)",
    "CREATE INDEX person_name       IF NOT EXISTS FOR (p:Person)  ON (p.name)",
]


def _parse_ceos(ceo_nm: str | None) -> list[str]:
    """company.json 의 ceo_nm 은 '전영현, 노태문' 같은 콤마 구분 문자열."""
    if not ceo_nm:
        return []
    return [n.strip() for n in ceo_nm.replace("·", ",").split(",") if n.strip()]


def load_graph_companies(
    *,
    dry_run: bool = False,
    batch_size: int = 200,
) -> LoadStats:
    """master.companies → Neo4j (Company + Market + Sector + Person)."""
    stats = LoadStats()
    s = get_settings()

    # PG 에서 회사 데이터 fetch
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
    sector_rows: list[dict[str, Any]] = []
    ceo_rows: list[dict[str, Any]] = []

    for corp_code, name, stock_code, market, sector, extra in all_rows:
        # extra 는 dict 또는 str(JSON)
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
            sector_rows.append({"corp_code": corp_code, "sector_code": sector})
        ceos = _parse_ceos(extra.get("ceo_nm"))
        if ceos:
            ceo_rows.append({"corp_code": corp_code, "ceos": ceos})

    if dry_run:
        stats.inserted = len(company_rows)
        stats.batches = (len(company_rows) + batch_size - 1) // batch_size
        stats.sql_preview.append(CYPHER_COMPANIES.strip())
        stats.sql_preview.append(
            f"-- companies={len(company_rows)} sectors={len(sector_rows)} ceos={len(ceo_rows)}"
        )
        return stats

    from ..db import neo4j as nx
    driver = nx.get_driver()

    def _batch(seq: list, n: int):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    with driver.session() as session:
        # 인덱스 먼저
        for cypher in CYPHER_INDEXES:
            session.run(cypher)

        # 회사 + 시장
        for batch in _batch(company_rows, batch_size):
            session.run(CYPHER_COMPANIES, rows=batch)
            stats.inserted += len(batch)
            stats.batches += 1

        # 섹터
        for batch in _batch(sector_rows, batch_size):
            session.run(CYPHER_SECTORS, rows=batch)
            stats.batches += 1

        # CEO
        for batch in _batch(ceo_rows, batch_size):
            session.run(CYPHER_CEOS, rows=batch)
            stats.batches += 1

    return stats
