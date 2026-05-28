"""P3 chunk 선별 — 자동차 도메인.

vec.chunks 에 자동차 메타 (manufacturer_id/model_id/variant_id) 와 source 컨벤션
('nhtsa_recall', 'nhtsa_complaint', 'wikipedia_auto', 'aihub_71347' …) 이 있으므로
finance 의 corp_code 필터와 분리한 별도 SELECT.

비용 가드 1단 (SQL) + 2단 (Python signal — 향후) 의 1단 만 본 모듈에서 처리.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from autonexusgraph.db.postgres import get_pool


log = logging.getLogger(__name__)


# 본 PR 의 P3 대상이 되는 vec.chunks.source 값들.
DEFAULT_SOURCES: tuple[str, ...] = (
    "nhtsa_recall",
    "nhtsa_complaint",
    "wikipedia_auto",
)


def select_auto_chunks(
    *,
    manufacturer_ids: Sequence[int] | None = None,
    model_ids: Sequence[int] | None = None,
    sources: Sequence[str] = DEFAULT_SOURCES,
    snapshot_years: Sequence[int] | None = None,
    min_token_count: int = 60,
    max_token_count: int = 2000,
    limit: int = 200,
    limit_per_manufacturer: int = 50,
) -> list[dict[str, Any]]:
    """vec.chunks 필터 — manufacturer_id/source/token 범위.

    Returns:
        각 dict 는 LLM 호출에 충분한 메타 + 본문:
            id, source, section, text, manufacturer_id, model_id, variant_id,
            metadata (jsonb 그대로).
    """
    where: list[str] = ["c.manufacturer_id IS NOT NULL"]
    params: list[Any] = []

    if sources:
        where.append("c.source = ANY(%s)")
        params.append(list(sources))
    if manufacturer_ids:
        where.append("c.manufacturer_id = ANY(%s)")
        params.append(list(manufacturer_ids))
    if model_ids:
        where.append("c.model_id = ANY(%s)")
        params.append(list(model_ids))
    where.append("c.token_count BETWEEN %s AND %s")
    params.extend([min_token_count, max_token_count])

    # window function: manufacturer 당 limit_per_manufacturer 까지만 (token_count 큰 순)
    sql = f"""
    WITH ranked AS (
      SELECT c.id, c.source, c.section, c.text, c.token_count,
             c.manufacturer_id, c.model_id, c.variant_id, c.metadata,
             row_number() OVER (PARTITION BY c.manufacturer_id
                                ORDER BY c.token_count DESC) AS rk
        FROM vec.chunks c
       WHERE {' AND '.join(where)}
    )
    SELECT id, source, section, text, token_count,
           manufacturer_id, model_id, variant_id, metadata
      FROM ranked
     WHERE rk <= %s
     ORDER BY manufacturer_id, model_id NULLS LAST, source, id
     LIMIT %s
    """
    params.extend([limit_per_manufacturer, limit])

    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    log.info("[p3.select] %d chunks (manufacturers=%s sources=%s)",
             len(rows),
             "all" if not manufacturer_ids else len(manufacturer_ids),
             sources)
    return rows


def resolve_manufacturer_name(manufacturer_id: int) -> str | None:
    """LLM 프롬프트 hint 용 — manufacturer_id → name."""
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM auto.master_manufacturers WHERE manufacturer_id = %s",
            (manufacturer_id,),
        )
        r = cur.fetchone()
        return r[0] if r else None


__all__ = ["DEFAULT_SOURCES", "select_auto_chunks", "resolve_manufacturer_name"]
