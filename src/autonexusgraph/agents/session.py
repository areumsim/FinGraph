"""경량 in-memory 세션 entity 메모리 — 흡수: _legacy/v1/src/agent/session_state.py.

PRD §7.6.2: Multi-turn 핵심 — "그 중", "위 회사들" 같은 reference 가 LLM rewriter 만으로
풀리지 않을 때 명시적 entity carry-over 가 안전망. triage 가 식별한 회사/인물/연도를
다음 turn 시작 시 자동 주입한다.

TTL: FINGRAPH_SESSION_TTL (기본 3600s)
LRU 한도: FINGRAPH_SESSION_MAX (기본 256)

multi-worker 환경에서는 워커별 상태 분리 — sticky session 또는 단일 worker 권장.
PG 영속화는 LangGraph checkpointer 가 담당 (PRD §7.5.8) — 이 모듈은 hot path 캐시.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace

logger = logging.getLogger(__name__)

_TTL_SECONDS = int(os.getenv("FINGRAPH_SESSION_TTL", "3600"))
_MAX_SESSIONS = int(os.getenv("FINGRAPH_SESSION_MAX", "256"))


@dataclass
class SessionState:
    """thread_id 단위로 보존되는 최소 컨텍스트."""

    # corp_code 목록 — 가장 최근 turn 에 사용된 회사들
    target_companies: list[str] = field(default_factory=list)
    # 인물명 목록 — 동명이인 분리(name, birth_year) 는 lookup 단계에서 처리
    target_persons: list[str] = field(default_factory=list)
    # AutoGraph 도메인 — variant/model id + manufacturer name (auto/cross_domain turn 보존)
    target_vehicles: list[int] = field(default_factory=list)
    target_models: list[int] = field(default_factory=list)
    target_makes: list[str] = field(default_factory=list)
    # 연도 hint
    last_year: int | None = None
    last_question_kind: str = ""
    last_question: str = ""
    updated_at: float = field(default_factory=time.time)


_SESSIONS: dict[str, SessionState] = {}
_LOCK = threading.Lock()


def _snapshot(state: SessionState) -> SessionState:
    """외부 변이·race 방지용 deep copy. list 도 새로 복사."""
    return replace(
        state,
        target_companies=list(state.target_companies),
        target_persons=list(state.target_persons),
        target_vehicles=list(state.target_vehicles),
        target_models=list(state.target_models),
        target_makes=list(state.target_makes),
    )


def _evict_expired(now: float) -> None:
    """TTL 초과 세션 제거 + LRU 한도 적용. update() 가 setdefault 전후로 호출한다."""
    expired = [sid for sid, st in _SESSIONS.items() if now - st.updated_at > _TTL_SECONDS]
    for sid in expired:
        _SESSIONS.pop(sid, None)


def _enforce_lru(touched: str) -> None:
    """LRU 한도 적용 — 방금 touch 한 세션은 제거 대상에서 제외."""
    if len(_SESSIONS) <= _MAX_SESSIONS:
        return
    ordered = sorted(
        ((sid, st) for sid, st in _SESSIONS.items() if sid != touched),
        key=lambda kv: kv[1].updated_at,
    )
    to_evict = len(_SESSIONS) - _MAX_SESSIONS
    for sid, _ in ordered[:to_evict]:
        _SESSIONS.pop(sid, None)


def get(thread_id: str) -> SessionState | None:
    """세션이 존재하고 TTL 내면 상태 반환. 없으면 None."""
    if not thread_id:
        return None
    now = time.time()
    with _LOCK:
        st = _SESSIONS.get(thread_id)
        if not st:
            return None
        if now - st.updated_at > _TTL_SECONDS:
            _SESSIONS.pop(thread_id, None)
            return None
        return _snapshot(st)


def update(
    thread_id: str,
    *,
    target_companies: list[str] | None = None,
    target_persons: list[str] | None = None,
    target_vehicles: list[int] | None = None,
    target_models: list[int] | None = None,
    target_makes: list[str] | None = None,
    last_year: int | None = None,
    last_question_kind: str = "",
    last_question: str = "",
) -> SessionState | None:
    """이번 turn 의 entity 식별 결과로 세션 갱신.

    none / 빈 인자는 기존 값을 보존 (덮어쓰지 않음).
    """
    if not thread_id:
        return None

    now = time.time()
    with _LOCK:
        _evict_expired(now)
        state = _SESSIONS.setdefault(thread_id, SessionState())

        if target_companies:
            state.target_companies = list(target_companies)
        if target_persons:
            state.target_persons = list(target_persons)
        if target_vehicles:
            state.target_vehicles = [int(v) for v in target_vehicles]
        if target_models:
            state.target_models = [int(v) for v in target_models]
        if target_makes:
            state.target_makes = list(target_makes)
        if last_year is not None:
            state.last_year = last_year
        if last_question_kind:
            state.last_question_kind = last_question_kind
        if last_question:
            state.last_question = last_question
        state.updated_at = now
        _enforce_lru(touched=thread_id)
        return _snapshot(state)


def summarize(state: SessionState | None) -> str:
    """프롬프트 주입용 — 이전 turn 의 entity 한 줄 요약."""
    if not state:
        return ""
    parts: list[str] = []
    if state.target_companies:
        parts.append(f"companies={','.join(state.target_companies[:5])}")
    if state.target_persons:
        parts.append(f"persons={','.join(state.target_persons[:3])}")
    if state.target_makes:
        parts.append(f"makes={','.join(state.target_makes[:3])}")
    if state.target_models:
        parts.append(f"models={','.join(str(m) for m in state.target_models[:3])}")
    if state.last_year is not None:
        parts.append(f"year={state.last_year}")
    return "; ".join(parts)


def clear(thread_id: str = "") -> None:
    """thread_id 1개 또는 전체 비우기 (테스트/관리)."""
    with _LOCK:
        if thread_id:
            _SESSIONS.pop(thread_id, None)
        else:
            _SESSIONS.clear()


__all__ = ["SessionState", "get", "update", "summarize", "clear"]
