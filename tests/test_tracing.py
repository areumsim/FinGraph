"""tracing 단위 테스트 — backend 결정 + fail-soft (SDK 미설치 / 키 누락)."""

from __future__ import annotations

from autonexusgraph.agents import tracing


def setup_function(_):
    tracing.reset_cache()


def test_backend_unset_returns_empty(monkeypatch):
    monkeypatch.delenv("TRACE_BACKEND", raising=False)
    # config 의 trace_backend 도 빈 값 (.env 미설정)
    assert tracing._resolve_backend() == ""


def test_backend_env_overrides_config(monkeypatch):
    monkeypatch.setenv("TRACE_BACKEND", "langsmith")
    assert tracing._resolve_backend() == "langsmith"


def test_backend_normalizes_case_and_off(monkeypatch):
    monkeypatch.setenv("TRACE_BACKEND", "  NONE  ")
    assert tracing._resolve_backend() == ""
    monkeypatch.setenv("TRACE_BACKEND", "LANGFUSE")
    tracing.reset_cache()
    assert tracing._resolve_backend() == "langfuse"


def test_describe_off(monkeypatch):
    monkeypatch.delenv("TRACE_BACKEND", raising=False)
    desc = tracing.describe_backend()
    assert "OFF" in desc


def test_describe_langsmith(monkeypatch):
    monkeypatch.setenv("TRACE_BACKEND", "langsmith")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setenv("LANGSMITH_PROJECT", "test-proj")
    desc = tracing.describe_backend()
    assert "langsmith" in desc
    assert "test-proj" in desc
    assert "set" in desc


def test_describe_langfuse_keys_missing(monkeypatch):
    monkeypatch.setenv("TRACE_BACKEND", "langfuse")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    desc = tracing.describe_backend()
    assert "langfuse" in desc
    assert "MISSING" in desc


def test_callbacks_empty_when_off(monkeypatch):
    monkeypatch.delenv("TRACE_BACKEND", raising=False)
    cbs = tracing.get_trace_callbacks()
    assert cbs == []


def test_callbacks_langsmith_returns_empty_list(monkeypatch):
    """LangSmith 는 env-flag 방식 — callbacks 리스트는 비어 있어야 함."""
    monkeypatch.setenv("TRACE_BACKEND", "langsmith")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    cbs = tracing.get_trace_callbacks()
    assert cbs == []
    # 부수효과: langchain 표준 env 가 채워졌는지
    import os
    assert os.getenv("LANGCHAIN_TRACING_V2") == "true"


def test_callbacks_langfuse_skips_if_sdk_missing(monkeypatch):
    """langfuse SDK 가 없으면 fail-soft — 빈 리스트."""
    monkeypatch.setenv("TRACE_BACKEND", "langfuse")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "x")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "y")
    cbs = tracing.get_trace_callbacks()
    # 환경에 langfuse 가 있으면 1개, 없으면 0개. 둘 다 정상.
    try:
        import langfuse  # noqa: F401
        assert isinstance(cbs, list)
    except ImportError:
        assert cbs == []


def test_callbacks_langfuse_keys_missing_returns_empty(monkeypatch):
    """langfuse SDK 가 있어도 키 없으면 callback 없음."""
    monkeypatch.setenv("TRACE_BACKEND", "langfuse")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    cbs = tracing.get_trace_callbacks()
    assert cbs == []


def test_callbacks_cached(monkeypatch):
    monkeypatch.setenv("TRACE_BACKEND", "langsmith")
    monkeypatch.setenv("LANGSMITH_API_KEY", "k1")
    cbs1 = tracing.get_trace_callbacks()
    cbs2 = tracing.get_trace_callbacks()
    assert cbs1 is cbs2


def test_callbacks_cache_invalidated_on_reset(monkeypatch):
    monkeypatch.setenv("TRACE_BACKEND", "langsmith")
    monkeypatch.setenv("LANGSMITH_API_KEY", "k1")
    tracing.get_trace_callbacks()
    tracing.reset_cache()
    monkeypatch.delenv("TRACE_BACKEND", raising=False)
    cbs = tracing.get_trace_callbacks()
    assert cbs == []
