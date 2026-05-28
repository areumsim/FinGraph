"""AutoGraph tools 공통 DB helper.

`spec.py`, `bridge.py` 의 ``conn = get_connection() / with conn.cursor() as cur /
cur.execute / cols = [...] / dict(zip(cols, r)) for r in cur.fetchall() /
conn.commit()`` 7회 반복 패턴을 단일 helper 로 통합.

- ``query_dicts(sql, params)`` → list[dict] (모든 row, dict)
- ``query_one_dict(sql, params)`` → dict | None (첫 row)
"""

from __future__ import annotations

from typing import Any, Sequence

from fingraph.db.postgres import get_connection


def query_dicts(sql: str, params: Sequence | dict | None = None) -> list[dict]:
    """READ-ONLY SELECT → list of dict. commit 후 반환 (사이드이펙트 없음)."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    return rows


def query_one_dict(sql: str, params: Sequence | dict | None = None) -> dict | None:
    """READ-ONLY SELECT → 첫 row dict, 없으면 None."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        r = cur.fetchone()
        if not r:
            conn.commit()
            return None
        cols = [d.name for d in cur.description]
    conn.commit()
    return dict(zip(cols, r))


__all__ = ["query_dicts", "query_one_dict"]
