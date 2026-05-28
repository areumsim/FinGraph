"""Hybrid 검색 도구 — pgvector 의미 검색 + 메타 필터.

에이전트의 RAG 입력. 자유 SQL/벡터 호출 금지(PRD §7.5.10).

핵심:
- search_documents(query, ...)  → 벡터 유사도 top-k + 메타 필터(year/source/section/corp)
- search_by_metadata(...)        → 필터만으로 청크 fetch (벡터 X)
- get_chunk(id)                  → 단일 청크 + 원문 메타

임베딩은 BGE-M3 (1024 dim, cosine). EMBEDDING_URL 서버 가동 필요.
"""
from __future__ import annotations

from typing import Any

from ..db.postgres import get_pool
from ..embeddings import EmbeddingError, get_embedding_client


DEFAULT_TOPK = 8
HARD_TOPK    = 50


def _cap(k: int | None) -> int:
    if k is None or k <= 0:
        return DEFAULT_TOPK
    return min(k, HARD_TOPK)


def _build_filter_clause(
    corp_code: str | list[str] | None,
    fiscal_year: int | None,
    fiscal_year_min: int | None,
    fiscal_year_max: int | None,
    source: str | list[str] | None,
    section_contains: str | None,
    report_type: str | None,
    *,
    require_embedding: bool = True,
) -> tuple[str, dict[str, Any]]:
    """WHERE 절 + named params 생성 — SQL injection 안전 (named placeholder).

    require_embedding=True 면 embedding NOT NULL 행만 (벡터 검색용),
    False 면 메타 필터만 — 임베딩 backfill 중에도 조회 가능.
    """
    clauses: list[str] = []
    if require_embedding:
        clauses.append("embedding IS NOT NULL")
    params: dict[str, Any] = {}
    if corp_code is not None:
        if isinstance(corp_code, str):
            clauses.append("corp_code = %(corp_code)s")
            params["corp_code"] = corp_code
        else:
            clauses.append("corp_code = ANY(%(corp_codes)s)")
            params["corp_codes"] = list(corp_code)
    if fiscal_year is not None:
        clauses.append("fiscal_year = %(fiscal_year)s")
        params["fiscal_year"] = fiscal_year
    if fiscal_year_min is not None:
        clauses.append("fiscal_year >= %(year_min)s")
        params["year_min"] = fiscal_year_min
    if fiscal_year_max is not None:
        clauses.append("fiscal_year <= %(year_max)s")
        params["year_max"] = fiscal_year_max
    if source is not None:
        if isinstance(source, str):
            clauses.append("source = %(source)s")
            params["source"] = source
        else:
            clauses.append("source = ANY(%(sources)s)")
            params["sources"] = list(source)
    if section_contains:
        clauses.append("section ILIKE %(section)s")
        params["section"] = f"%{section_contains}%"
    if report_type:
        clauses.append("report_type = %(report_type)s")
        params["report_type"] = report_type
    return " AND ".join(clauses), params


def search_documents(
    query: str,
    *,
    top_k: int = DEFAULT_TOPK,
    corp_code: str | list[str] | None = None,
    fiscal_year: int | None = None,
    fiscal_year_min: int | None = None,
    fiscal_year_max: int | None = None,
    source: str | list[str] | None = None,
    section_contains: str | None = None,
    report_type: str | None = None,
) -> list[dict]:
    """벡터 유사도 검색 + 메타 필터.

    리턴 row 키:
      id, corp_code, rcept_no, source, section, report_type, fiscal_year,
      chunk_idx, text, score(cosine sim 0~1), token_count
    """
    if not query or not query.strip():
        return []

    client = get_embedding_client()
    try:
        qvec = client.embed_one(query)
    except EmbeddingError as e:
        raise RuntimeError(
            f"임베딩 호출 실패. BGE-M3 서버(EMBEDDING_URL) 가동 확인. {e}"
        ) from e

    where, params = _build_filter_clause(
        corp_code, fiscal_year, fiscal_year_min, fiscal_year_max,
        source, section_contains, report_type,
        require_embedding=True,
    )
    params["q"] = qvec
    params["k"] = _cap(top_k)

    sql = f"""
    SELECT id, corp_code, rcept_no, source, section, report_type,
           fiscal_year, chunk_idx, text, token_count,
           1 - (embedding <=> %(q)s::vector) AS score
      FROM vec.chunks
     WHERE {where}
     ORDER BY embedding <=> %(q)s::vector
     LIMIT %(k)s
    """
    pool = get_pool()
    from pgvector.psycopg import register_vector
    with pool.connection() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def search_by_metadata(
    *,
    corp_code: str | list[str] | None = None,
    fiscal_year: int | None = None,
    section_contains: str | None = None,
    source: str | list[str] | None = None,
    report_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """필터만으로 청크 fetch — 벡터 검색 미사용.

    "삼성전자 2024년 사업보고서 위험요인 섹션 전체" 같은 결정적 fetch 시 유용.
    """
    where, params = _build_filter_clause(
        corp_code, fiscal_year, None, None, source, section_contains, report_type,
        require_embedding=False,
    )
    if not where:
        where = "TRUE"
    params["limit"] = max(1, min(int(limit), 500))
    sql = f"""
    SELECT id, corp_code, rcept_no, source, section, report_type,
           fiscal_year, chunk_idx, text, token_count
      FROM vec.chunks
     WHERE {where}
     ORDER BY corp_code, fiscal_year DESC, chunk_idx
     LIMIT %(limit)s
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_chunk(chunk_id: int) -> dict | None:
    """단일 청크 + 메타 (id 로 조회)."""
    sql = """
    SELECT id, corp_code, rcept_no, source, section, report_type,
           fiscal_year, chunk_idx, text, token_count, metadata
      FROM vec.chunks
     WHERE id = %s
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (chunk_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d.name for d in cur.description]
        return dict(zip(cols, row))


__all__ = [
    "search_documents",
    "search_by_metadata",
    "get_chunk",
]
