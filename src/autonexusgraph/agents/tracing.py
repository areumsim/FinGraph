"""Langfuse + LangSmith 통합 — PRD §7.5.11.

설계:
- env / config 의 TRACE_BACKEND 로 한 백엔드 선택. 둘 다 설정 시 langfuse 우선.
- get_callbacks() — langgraph app.invoke/stream 의 config={"callbacks": [...]} 주입용
- 모든 import / 키 누락 / 초기화 실패는 fail-soft (silent skip + warning log)
- LangSmith 는 LANGCHAIN_TRACING_V2=true 만 켜도 langgraph 가 자동 전송 → callback 불필요

호출 패턴:
    from .tracing import get_trace_callbacks
    callbacks = get_trace_callbacks()
    app.invoke(state, config={"configurable": {...}, "callbacks": callbacks})
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_BACKEND_CACHE: str | None = None
_CALLBACK_CACHE: list[Any] | None = None


def _resolve_backend() -> str:
    """env > config > 빈값. 결과: 'langfuse' | 'langsmith' | ''."""
    raw = os.getenv("TRACE_BACKEND")
    if raw is None or raw == "":
        try:
            from ..config import get_settings
            raw = get_settings().trace_backend or ""
        except Exception:
            raw = ""
    raw = (raw or "").strip().lower()
    if raw in ("none", "off"):
        return ""
    return raw


def describe_backend() -> str:
    """헬스체크용 — 현 환경의 tracing 활성 여부 + 백엔드 한 줄."""
    backend = _resolve_backend()
    if not backend:
        return "tracing: OFF (TRACE_BACKEND 비어 있음)"
    if backend == "langfuse":
        host = os.getenv("LANGFUSE_HOST") or "cloud.langfuse.com"
        key = "set" if (os.getenv("LANGFUSE_PUBLIC_KEY") or os.getenv("LANGFUSE_SECRET_KEY")) else "MISSING"
        return f"tracing: langfuse host={host} keys={key}"
    if backend == "langsmith":
        key = "set" if os.getenv("LANGSMITH_API_KEY") else "MISSING"
        proj = os.getenv("LANGSMITH_PROJECT") or "autonexusgraph"
        return f"tracing: langsmith project={proj} key={key}"
    return f"tracing: unknown backend '{backend}'"


def get_trace_callbacks() -> list[Any]:
    """app.invoke 의 config['callbacks'] 에 넣을 핸들러 리스트.

    백엔드별:
    - langfuse: langfuse.callback.CallbackHandler — invoke마다 trace
    - langsmith: LANGCHAIN_TRACING_V2=true 환경변수로 자동 전송 → callback 불필요 ([] 반환)
    - 그 외: []
    """
    global _CALLBACK_CACHE, _BACKEND_CACHE
    backend = _resolve_backend()
    # cache invalidation when backend changes
    if _CALLBACK_CACHE is not None and _BACKEND_CACHE == backend:
        return _CALLBACK_CACHE

    _BACKEND_CACHE = backend
    cbs: list[Any] = []

    if backend == "langfuse":
        cb = _build_langfuse_callback()
        if cb is not None:
            cbs.append(cb)
    elif backend == "langsmith":
        _enable_langsmith_env()
        # langgraph 가 자동으로 LangSmith 로 전송 — 추가 callback 불필요

    _CALLBACK_CACHE = cbs
    return cbs


def _build_langfuse_callback() -> Any | None:
    """langfuse.callback.CallbackHandler — 키 + SDK 모두 있어야 활성."""
    try:
        from langfuse.callback import CallbackHandler   # type: ignore[import-not-found]
    except ImportError:
        try:
            # langfuse 3.x 이름이 바뀐 경우 대비
            from langfuse import Langfuse   # type: ignore[import-not-found]
            from langfuse.langchain import CallbackHandler   # type: ignore[import-not-found]
            _ = Langfuse  # noqa: F841
        except ImportError:
            logger.debug("langfuse 미설치 — callback skip")
            return None
    pub = os.getenv("LANGFUSE_PUBLIC_KEY")
    sec = os.getenv("LANGFUSE_SECRET_KEY")
    host = os.getenv("LANGFUSE_HOST") or None
    if not (pub and sec):
        logger.warning("LANGFUSE_PUBLIC_KEY/SECRET_KEY 미설정 — langfuse callback skip")
        return None
    try:
        kwargs = {"public_key": pub, "secret_key": sec}
        if host:
            kwargs["host"] = host
        return CallbackHandler(**kwargs)
    except Exception as exc:   # noqa: BLE001
        logger.warning("Langfuse CallbackHandler 초기화 실패 (skip): %s", exc)
        return None


def _enable_langsmith_env() -> None:
    """LangSmith 자동 트레이스 — 환경변수 보강."""
    if not os.getenv("LANGSMITH_API_KEY"):
        logger.warning("LANGSMITH_API_KEY 미설정 — tracing 신호 안 보내질 수 있음")
        return
    # langchain 표준 변수
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", os.getenv("LANGSMITH_API_KEY", ""))
    proj = os.getenv("LANGSMITH_PROJECT")
    if proj:
        os.environ.setdefault("LANGCHAIN_PROJECT", proj)


def reset_cache() -> None:
    """테스트에서 backend env 바꾼 뒤 캐시 무효화."""
    global _CALLBACK_CACHE, _BACKEND_CACHE
    _CALLBACK_CACHE = None
    _BACKEND_CACHE = None


# ── 도메인 trace 태그 / metadata 헬퍼 ──────────────────────
def tags_for_domain(domain: str | None) -> list[str]:
    """LangGraph config['tags'] 에 들어갈 도메인 식별 태그.

    Langfuse / LangSmith UI 에서 trace 필터링에 사용. autograph 분기가 발화한 turn
    을 finance 와 분리해 모니터링 가능.
    """
    d = (domain or "").strip().lower() or "finance"
    base = ["autonexusgraph", f"domain:{d}"]
    if d == "auto":
        base.append("autograph")
    elif d == "cross_domain":
        # 양쪽 모두 발화하므로 두 tag 모두 부착.
        base.append("autograph")
    return base


def metadata_for_state(state: dict) -> dict:
    """LangGraph config['metadata'] 에 합쳐질 trace metadata.

    state 의 비-PII 식별자만 추출 — domain/thread_id/target 카운트.
    재무 회사 corp_code / 자동차 variant_id 같은 raw id 는 length 만 노출.
    """
    if not isinstance(state, dict):
        return {"domain": "finance"}
    domain = str(state.get("domain") or "finance").lower()
    md: dict = {
        "domain": domain,
        "n_target_companies": len(state.get("target_companies") or []),
        "n_target_vehicles":  len(state.get("target_vehicles") or []),
        "n_target_models":    len(state.get("target_models") or []),
        "n_history":          len(state.get("history") or []),
    }
    # question_kind 가 triage 후에 채워지면 함께 노출.
    if state.get("question_kind"):
        md["question_kind"] = state["question_kind"]
    return md


__all__ = [
    "get_trace_callbacks",
    "describe_backend",
    "reset_cache",
    "tags_for_domain",
    "metadata_for_state",
]
