"""런타임 LLM 비용 트래커 + circuit breaker.

singleton — 프로세스 1회 instance. thread-safe.

설계 원칙 (사용자 명시 요구):
- 모든 LLM adapter 의 chat()/chat_stream()/chat_json() 호출 후 tracker.record() 호출.
- 누적 비용이 hard_limit 도달 시 다음 호출에서 BudgetExceeded raise → batch abort.
- 매 N 호출마다 누적 비용 로그.
- 시작 시 ops.llm_usage 에 run row 생성, 종료 시 status / 총합 update.

사용 패턴:
    from autonexusgraph.llm.cost_tracker import get_tracker, BudgetExceeded

    tracker = get_tracker(caller='p3_extract', model='gpt-4o-mini')
    try:
        for item in items:
            tracker.guard()                  # 한도 초과면 BudgetExceeded
            resp = llm.chat(...)
            tracker.record(resp.usage_input, resp.usage_output, model='gpt-4o-mini')
    except BudgetExceeded:
        ...                                  # state 저장 후 종료
    finally:
        tracker.finalize(status='ok' or 'aborted_budget')
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from .cost import cost_of_call, get_hard_limit_usd, get_report_every


log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    """누적 비용이 hard_limit 도달 — batch abort 신호."""


@dataclass
class TrackerState:
    run_id: str
    caller: str
    model: str
    hard_limit_usd: float
    n_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    aborted: bool = False
    finalized: bool = False


class CostTracker:
    """프로세스 단위 LLM 비용 누적 + 한도 가드."""

    def __init__(self, caller: str, model: str, hard_limit: float | None = None) -> None:
        limit = hard_limit if hard_limit is not None else get_hard_limit_usd()
        self.state = TrackerState(
            run_id=str(uuid.uuid4()),
            caller=caller,
            model=model,
            hard_limit_usd=limit,
        )
        self._lock = threading.Lock()
        self._report_every = get_report_every()
        self._persist_initial()

    # ── 누적 ───────────────────────────────────────────────────
    def record(self, input_tokens: int, output_tokens: int,
               model: str | None = None, *, purpose: str | None = None,
               latency_ms: int | None = None) -> None:
        """단일 호출 사용량 기록 + 한도 체크 (post-record)."""
        m = model or self.state.model
        c = cost_of_call(m, input_tokens, output_tokens)
        with self._lock:
            self.state.n_calls += 1
            self.state.input_tokens += input_tokens
            self.state.output_tokens += output_tokens
            self.state.cost_usd += c
            n = self.state.n_calls
            cum = self.state.cost_usd
            limit = self.state.hard_limit_usd

        # call detail 비동기 적재는 옵션. 기본은 끔 (대량 호출 시 부하).
        if os.environ.get("LLM_COST_LOG_CALLS") == "1":
            self._persist_call(m, input_tokens, output_tokens, c,
                               purpose=purpose, latency_ms=latency_ms)

        if n % self._report_every == 0 or cum >= limit * 0.9:
            log.info(f"[COST] {self.state.caller} n_calls={n} cum=${cum:.4f} "
                     f"(limit ${limit:.4f}, {100*cum/max(limit,1e-9):.1f}%)")

    def guard(self) -> None:
        """다음 호출 전에 한도 확인. 초과 시 BudgetExceeded."""
        with self._lock:
            if self.state.cost_usd >= self.state.hard_limit_usd:
                self.state.aborted = True
                raise BudgetExceeded(
                    f"누적 비용 ${self.state.cost_usd:.4f} ≥ hard_limit "
                    f"${self.state.hard_limit_usd:.4f} (caller={self.state.caller})"
                )

    # ── 종료 ──────────────────────────────────────────────────
    def finalize(self, status: str = "ok") -> None:
        """run 종료 — ops.llm_usage 에 ended_at/총합/status update."""
        with self._lock:
            if self.state.finalized:
                return
            if self.state.aborted and status == "ok":
                status = "aborted_budget"
            self.state.finalized = True
        self._persist_final(status)
        log.info(f"[COST] FINAL caller={self.state.caller} status={status} "
                 f"n_calls={self.state.n_calls} cost=${self.state.cost_usd:.4f}")

    # ── DB 적재 (모두 best-effort — DB 다운 시 추적은 메모리에만) ─────
    def _persist_initial(self) -> None:
        try:
            from ..db.postgres import get_pool
            with get_pool().connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ops.llm_usage (run_id, caller, model, status)
                    VALUES (%s, %s, %s, 'running')
                    """,
                    (self.state.run_id, self.state.caller, self.state.model),
                )
        except Exception as e:
            log.warning(f"[COST] llm_usage init persist failed: {e}")

    def _persist_final(self, status: str) -> None:
        try:
            from ..db.postgres import get_pool
            with get_pool().connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ops.llm_usage
                       SET ended_at      = now(),
                           n_calls       = %s,
                           input_tokens  = %s,
                           output_tokens = %s,
                           cost_usd      = %s,
                           status        = %s
                     WHERE run_id = %s
                    """,
                    (self.state.n_calls, self.state.input_tokens,
                     self.state.output_tokens, self.state.cost_usd,
                     status, self.state.run_id),
                )
        except Exception as e:
            log.warning(f"[COST] llm_usage final persist failed: {e}")

    def _persist_call(self, model: str, input_tokens: int, output_tokens: int,
                       cost: float, *, purpose: str | None,
                       latency_ms: int | None) -> None:
        try:
            from ..db.postgres import get_pool
            with get_pool().connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ops.llm_calls
                      (run_id, model, purpose, input_tokens, output_tokens,
                       cost_usd, latency_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (self.state.run_id, model, purpose, input_tokens,
                     output_tokens, cost, latency_ms),
                )
        except Exception as e:
            log.debug(f"[COST] llm_calls persist failed: {e}")

    # ── 컨텍스트 매니저 ────────────────────────────────────────
    def __enter__(self) -> "CostTracker":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is BudgetExceeded:
            self.finalize("aborted_budget")
        elif exc_type is not None:
            self.finalize("error")
        else:
            self.finalize("ok")


# ─── 프로세스 싱글톤 ────────────────────────────────────────────
_singleton: CostTracker | None = None
_singleton_lock = threading.Lock()


def get_tracker(caller: str, model: str,
                 hard_limit: float | None = None) -> CostTracker:
    """프로세스 안에서 1개 tracker 만. 새 run 시작 시 reset_tracker() 호출 후 get."""
    global _singleton
    with _singleton_lock:
        if _singleton is None or _singleton.state.finalized:
            _singleton = CostTracker(caller=caller, model=model, hard_limit=hard_limit)
        return _singleton


def reset_tracker() -> None:
    """이전 tracker finalize 후 새 run 시작 준비."""
    global _singleton
    with _singleton_lock:
        if _singleton and not _singleton.state.finalized:
            _singleton.finalize("ok")
        _singleton = None


__all__ = ["CostTracker", "BudgetExceeded", "get_tracker", "reset_tracker"]
