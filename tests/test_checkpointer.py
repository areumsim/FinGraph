"""checkpointer 단위 테스트 — DSN 우선순위 + search_path 인코딩.

langgraph 미설치 환경에서도 동작하도록 fail-soft 경로를 모킹.
"""

from __future__ import annotations

import pytest

from autonexusgraph.agents import checkpointer


def test_search_path_injection_no_query():
    out = checkpointer._inject_search_path(
        "postgresql://u:p@h:5432/db", "chat"
    )
    # search_path 가 query string 에 포함됐는지
    assert "options=" in out
    assert "search_path" in out
    assert "chat" in out
    assert "public" in out


def test_search_path_injection_existing_query():
    """기존 query 가 있어도 options 만 추가/덮어쓰기."""
    out = checkpointer._inject_search_path(
        "postgresql://u:p@h:5432/db?sslmode=require", "chat"
    )
    assert "sslmode=require" in out
    assert "options=" in out
    assert "search_path" in out


def test_search_path_injection_existing_options_overwritten():
    """기존 options 가 있으면 search_path 로 덮어씀."""
    out = checkpointer._inject_search_path(
        "postgresql://u:p@h:5432/db?options=-csomething%3Delse", "chat"
    )
    # 새 search_path 가 들어가야 함 (기존 -csomething=else 는 단순 덮어쓰기 정책)
    assert "search_path" in out
    assert "chat" in out


def test_redact_password():
    assert "***" in checkpointer._redact("postgresql://user:secret@host/db")
    assert "secret" not in checkpointer._redact("postgresql://user:secret@host/db")


def test_resolve_dsn_priority_env_first(monkeypatch):
    """FINGRAPH_PG_DSN env 가 최우선."""
    monkeypatch.setenv("FINGRAPH_PG_DSN", "postgresql://env1:p@h/db1")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://env2:p@h/db2")
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DSN", "")
    assert checkpointer._resolve_dsn() == "postgresql://env1:p@h/db1"


def test_resolve_dsn_priority_langgraph_specific(monkeypatch):
    """LANGGRAPH_CHECKPOINT_DSN 이 가장 specific — 1순위."""
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_DSN", "postgresql://lg:p@h/lgdb")
    monkeypatch.setenv("FINGRAPH_PG_DSN", "postgresql://env1:p@h/db1")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://env2:p@h/db2")
    assert checkpointer._resolve_dsn() == "postgresql://lg:p@h/lgdb"


def test_resolve_dsn_fallback_to_postgres_dsn(monkeypatch):
    """LangGraph 전용 dsn 없으면 POSTGRES_DSN 사용."""
    monkeypatch.delenv("LANGGRAPH_CHECKPOINT_DSN", raising=False)
    monkeypatch.delenv("FINGRAPH_PG_DSN", raising=False)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://main:p@h/maindb")
    assert checkpointer._resolve_dsn() == "postgresql://main:p@h/maindb"


def test_resolve_backend_none_returns_none(monkeypatch):
    """backend=none 이면 get_checkpointer() 가 None 반환."""
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_BACKEND", "none")
    assert checkpointer.get_checkpointer() is None


def test_resolve_backend_memory_returns_none_if_lg_missing(monkeypatch):
    """backend=memory 인데 langgraph 미설치면 None (fail-soft)."""
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_BACKEND", "memory")
    # 현 환경엔 langgraph 없으므로 None
    result = checkpointer.get_checkpointer()
    try:
        import langgraph  # noqa: F401
        assert result is not None
    except ImportError:
        assert result is None


def test_resolve_schema_default(monkeypatch):
    monkeypatch.delenv("LANGGRAPH_CHECKPOINT_SCHEMA", raising=False)
    s = checkpointer._resolve_schema()
    assert s == "chat"


def test_resolve_schema_env_override(monkeypatch):
    monkeypatch.setenv("LANGGRAPH_CHECKPOINT_SCHEMA", "langgraph")
    assert checkpointer._resolve_schema() == "langgraph"
