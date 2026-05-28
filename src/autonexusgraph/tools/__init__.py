"""에이전트 도구 (PRD §7.5.2 / §7.5.10).

자유 SQL/Cypher/벡터 호출 금지 — 사전 정의 함수 풀만 노출.
LLM 은 함수명 + 파라미터만 결정 → SQL injection / 그래프 폭발 / 토큰 폭발 차단.

모듈 구성:
- financials : PG 정형 (재무수치, 회사 마스터)
- graph      : Neo4j 그래프 탐색 (자회사·임원·주주·뉴스·기업집단)
- retrieve   : Hybrid 검색 (pgvector 의미 검색 + 메타 필터)
"""

from .financials import (
    compare_companies,
    get_balance_sheet_item,
    get_company_info,
    get_operating_income,
    get_revenue,
    list_companies_by_market,
    lookup_company,
)
from .graph import (
    find_paths,
    get_companies_of_person,
    get_executives,
    get_major_shareholders,
    get_subgraph,
    list_cooccurring,
    list_group_members,
    list_mentioning_news,
    list_parents,
    list_subsidiaries,
    lookup_person,
)
from .retrieve import (
    get_chunk,
    search_by_metadata,
    search_documents,
)

__all__ = [
    # financials
    "lookup_company",
    "get_company_info",
    "get_revenue",
    "get_operating_income",
    "get_balance_sheet_item",
    "compare_companies",
    "list_companies_by_market",
    # graph
    "lookup_person",
    "list_subsidiaries",
    "list_parents",
    "get_executives",
    "get_companies_of_person",
    "get_major_shareholders",
    "find_paths",
    "get_subgraph",
    "list_mentioning_news",
    "list_cooccurring",
    "list_group_members",
    # retrieve
    "search_documents",
    "search_by_metadata",
    "get_chunk",
]
