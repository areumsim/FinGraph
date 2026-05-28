"""LangGraph checkpointer 헬퍼 — PG (chat 스키마) 우선 → in-memory fallback.

PRD §7.5.8 — 모든 State 는 PostgreSQL 체크포인트로 저장돼 시스템 중단 시
마지막 노드부터 재개 가능해야 한다.

DSN 우선순위 (높은 → 낮은):
1. config.langgraph_checkpoint_dsn       — 별도 PG 풀 쓰고 싶을 때
2. env FINGRAPH_PG_DSN / POSTGRES_DSN   — 기존 메인 DSN
3. config.postgres_dsn                   — autonexusgraph 표준 PG DSN

스키마: config.langgraph_checkpoint_schema (기본 'chat'). PostgresSaver 가
기본적으로 public 에 테이블을 만들기 때문에 dsn options 로 search_path 를
chat,public 으로 주입하고, 사전에 CREATE SCHEMA IF NOT EXISTS chat 를 보장한다.

import 우선순위:
1. langgraph.checkpoint.postgres.PostgresSaver — 실제 PG 영속화
2. langgraph.checkpoint.memory.MemorySaver     — langgraph 만 있고 PG 패키지 없음
3. None                                          — langgraph 자체가 없음 → 함수 체인 폴백

사용:
    from .checkpointer import get_checkpointer
    cp = get_checkpointer()
    app = workflow.compile(checkpointer=cp)
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote_plus, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)


def get_checkpointer() -> Any | None:
    """현 환경에서 사용 가능한 checkpointer 반환."""
    backend = _resolve_backend()
    if backend == "none":
        return None
    if backend in ("memory", "in_memory"):
        return _memory_saver()
    # auto: PG 시도 → memory 폴백
    saver = _postgres_saver()
    if saver is not None:
        return saver
    return _memory_saver()


def _resolve_backend() -> str:
    """env > config > 기본값('auto')."""
    raw = os.getenv("LANGGRAPH_CHECKPOINT_BACKEND") \
        or os.getenv("FINGRAPH_LANGGRAPH_CHECKPOINT_BACKEND")   # backward compat
    if raw:
        return raw.lower()
    try:
        from ..config import get_settings
        return get_settings().langgraph_checkpoint_backend
    except Exception:
        return "auto"


def _resolve_schema() -> str:
    """checkpoint 테이블 스키마 — 기본 'chat'."""
    raw = os.getenv("LANGGRAPH_CHECKPOINT_SCHEMA")
    if raw:
        return raw
    try:
        from ..config import get_settings
        return get_settings().langgraph_checkpoint_schema or "chat"
    except Exception:
        return "chat"


def _resolve_dsn() -> str | None:
    """DSN 우선순위: config.langgraph_checkpoint_dsn → env → config.postgres_dsn."""
    # 1) 전용 dsn (env or config)
    raw = os.getenv("LANGGRAPH_CHECKPOINT_DSN") or os.getenv("FINGRAPH_PG_DSN")
    if raw:
        return raw
    try:
        from ..config import get_settings
        s = get_settings()
        if getattr(s, "langgraph_checkpoint_dsn", ""):
            return s.langgraph_checkpoint_dsn
    except Exception:
        pass
    # 2) POSTGRES_DSN env
    raw = os.getenv("POSTGRES_DSN")
    if raw:
        return raw
    # 3) config.postgres_dsn
    try:
        from ..config import get_settings
        return get_settings().postgres_dsn or None
    except Exception:
        return None


def _inject_search_path(dsn: str, schema: str) -> str:
    """dsn 끝에 ?options=-csearch_path%3D<schema>%2Cpublic 추가.

    이미 options 가 있으면 search_path 만 덮어쓴다.
    """
    if not dsn:
        return dsn
    parsed = urlparse(dsn)
    # query 파싱 — psycopg URI 의 ?options=... 또는 다른 키 보존
    from urllib.parse import parse_qsl
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    search_opt = f"-csearch_path={schema},public"
    # 기존 options 가 있으면 search_path 부분만 교체, 없으면 추가
    new_pairs: list[tuple[str, str]] = []
    found = False
    for k, v in pairs:
        if k == "options":
            # 단순 덮어쓰기 — 다른 options 가 있어도 search_path 우선 정렬
            new_pairs.append((k, search_opt))
            found = True
        else:
            new_pairs.append((k, v))
    if not found:
        new_pairs.append(("options", search_opt))
    new_query = urlencode(new_pairs, quote_via=quote_plus)
    return urlunparse(parsed._replace(query=new_query))


def _ensure_schema_exists(dsn: str, schema: str) -> bool:
    """CREATE SCHEMA IF NOT EXISTS <schema>. 실패 시 False (보통 권한 문제)."""
    try:
        import psycopg
    except ImportError:
        logger.warning("psycopg 미설치 — schema 보장 skip")
        return False
    try:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            # 식별자는 quote_ident 로 안전 처리
            from psycopg.sql import SQL, Identifier
            cur.execute(SQL("CREATE SCHEMA IF NOT EXISTS {}").format(Identifier(schema)))
        return True
    except Exception as exc:   # noqa: BLE001
        logger.warning("CREATE SCHEMA %s 실패 (계속 진행, public 사용 가능): %s", schema, exc)
        return False


def _postgres_saver() -> Any | None:
    """PG 기반 PostgresSaver. dsn + 패키지 모두 있어야 사용.

    langgraph-checkpoint-postgres 3.x 는 ``from_conn_string`` 이 contextmanager
    (생성자가 yield 후 close) 이므로 영구 saver 가 필요한 우리는 직접 psycopg
    connection 을 만들어 ``PostgresSaver(conn)`` 으로 인스턴스화한다.
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError:
        logger.debug("langgraph-checkpoint-postgres 미설치 — PG saver skip")
        return None
    try:
        import psycopg
    except ImportError:
        logger.warning("psycopg 미설치 — PG saver skip (pyproject [db] extra 필요)")
        return None

    dsn = _resolve_dsn()
    if not dsn:
        logger.warning("PG DSN 미설정 — memory saver 폴백")
        return None

    schema = _resolve_schema()
    _ensure_schema_exists(dsn, schema)
    dsn_with_path = _inject_search_path(dsn, schema)

    try:
        # autocommit + row_factory 는 PostgresSaver 가 요구 (3.x).
        conn = psycopg.Connection.connect(
            dsn_with_path,
            autocommit=True,
            prepare_threshold=0,
            row_factory=psycopg.rows.dict_row,
        )
        saver = PostgresSaver(conn)
        try:
            saver.setup()
        except Exception as exc:   # noqa: BLE001
            logger.warning("PostgresSaver.setup() 실패 (계속 진행): %s", exc)
        logger.info("LangGraph PostgresSaver 활성 (schema=%s, dsn=%s)",
                    schema, _redact(dsn_with_path))
        return saver
    except Exception as exc:   # noqa: BLE001 — fail-soft
        logger.warning("PostgresSaver 초기화 실패 — memory 폴백: %s", exc)
        return None


def _memory_saver() -> Any | None:
    """MemorySaver. langgraph 있을 때만 — 없으면 None (run_agent 가 함수 체인 사용)."""
    try:
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()
    except ImportError:
        return None


def _redact(dsn: str) -> str:
    """비밀번호 가린 dsn — 로그용."""
    import re
    return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", dsn)


__all__ = ["get_checkpointer"]
