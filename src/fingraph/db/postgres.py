"""PostgreSQL 연결 헬퍼.

PRD §4.3 — PostgreSQL은 정확한 수치(재무) 저장소 + LangGraph 체크포인트.
psycopg3 사용 (psycopg[binary,pool]).
"""

from __future__ import annotations

from functools import lru_cache

from ..config import get_settings


@lru_cache(maxsize=1)
def get_connection():
    """psycopg.Connection 싱글톤 — 단순 ping용. 실제 사용은 pool 권장.

    psycopg 패키지가 설치되어 있어야 한다 (pip install '.[db]').
    """
    import psycopg

    s = get_settings()
    return psycopg.connect(s.postgres_dsn)


def ping() -> bool:
    """연결 헬스체크."""
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1
    except Exception:
        return False


def close() -> None:
    if get_connection.cache_info().currsize > 0:
        get_connection().close()
        get_connection.cache_clear()
