"""PostgreSQL 연결 헬퍼.

PRD §4.3 — PostgreSQL은 정확한 수치(재무) 저장소 + LangGraph 체크포인트.
psycopg3 사용 (psycopg[binary,pool]).

용도별 4가지:
- get_connection(): 단순 1-회 연결 (스크립트 / ping)
- get_pool(): 동시성 필요 시 (API/agent)
- transaction(): with 블록 context manager
- copy_from(): 대량 적재용 (loaders 가 사용)
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

from ..config import get_settings


@lru_cache(maxsize=1)
def get_connection() -> Any:
    """psycopg.Connection 싱글톤 — 단순 작업용.

    psycopg 패키지가 설치되어 있어야 한다 (pip install '.[db]').
    """
    import psycopg
    s = get_settings()
    return psycopg.connect(s.postgres_dsn)


@lru_cache(maxsize=1)
def get_pool():
    """ConnectionPool 싱글톤 — 동시성 필요 시 (FastAPI, agent)."""
    from psycopg_pool import ConnectionPool
    s = get_settings()
    return ConnectionPool(s.postgres_dsn, min_size=2, max_size=10, open=True)


def ping() -> bool:
    """연결 헬스체크."""
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1
    except Exception:
        return False


@contextmanager
def transaction() -> Iterator[Any]:
    """싱글톤 connection 의 트랜잭션. with 블록 종료 시 commit/rollback.

    사용:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close() -> None:
    """싱글톤 정리 (테스트 cleanup)."""
    if get_connection.cache_info().currsize > 0:
        get_connection().close()
        get_connection.cache_clear()
    if get_pool.cache_info().currsize > 0:
        get_pool().close()
        get_pool.cache_clear()
