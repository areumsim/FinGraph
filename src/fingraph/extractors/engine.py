"""ExtractorEngine — 다단 추출기 병렬 실행 + circuit breaker + merge.

설계 메모 (이전 v2/extractors/engine.py 의 핵심 패턴 흡수):
- ThreadPoolExecutor 병렬 실행 (extractor 당 timeout 적용)
- circuit breaker: 동일 추출기가 MAX_FAIL_STREAK 회 연속 실패 시 COOLDOWN_S 동안 차단
- merge: (head, relation, tail, fiscal_year) 키로 dedupe + evidence 누적
- BudgetExceeded 는 즉시 전파 (배치 abort 신호 — 비용 가드)

우리 도메인 단순화:
- entity 추출 없음 (P2 정형 SSOT). relation 만.
- chunk 입력 dict 그대로. 추출기 출력은 ExtractorResult.relations.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .base import BaseExtractor, ExtractorResult, RunContext
from ..llm.cost_tracker import BudgetExceeded


log = logging.getLogger(__name__)


MAX_FAIL_STREAK = 3              # 연속 실패 N 회 → circuit open
COOLDOWN_S = 30.0                # cooldown 시간


@dataclass
class _CircuitState:
    """단일 추출기의 circuit breaker 상태."""
    fail_streak: int = 0
    cooled_until: float = 0.0     # monotonic time 기준


@dataclass
class EngineRunStats:
    """엔진 run 통계."""
    n_chunks: int = 0
    n_extractor_calls: int = 0
    n_circuit_blocks: int = 0
    n_warnings: int = 0
    total_latency_ms: int = 0


class ExtractorEngine:
    """다중 추출기 병렬 실행 + dedupe merge.

    사용:
        engine = ExtractorEngine([LLMRelationExtractor(), RegexExtractor()])
        for chunk in chunks:
            relations, stats = engine.process(chunk, ctx)
    """

    def __init__(
        self,
        extractors: Sequence[BaseExtractor],
        *,
        max_concurrency: int | None = None,
    ) -> None:
        if not extractors:
            raise ValueError("ExtractorEngine 에는 최소 1개의 추출기가 필요")
        self.extractors: list[BaseExtractor] = list(extractors)
        self.max_concurrency = max_concurrency or min(len(self.extractors), 4)
        self._circuits: dict[str, _CircuitState] = {
            e.name: _CircuitState() for e in self.extractors
        }
        self.stats = EngineRunStats()

    # ── public API ─────────────────────────────────────────
    def process(self, chunk: dict, ctx: RunContext) -> tuple[list[dict], list[ExtractorResult]]:
        """단일 청크 → 병렬 추출 → merged relations + 원본 결과 list 반환.

        BudgetExceeded 는 그대로 raise (배치 단위 abort).
        다른 예외는 safe_extract 가 흡수 → warning 으로 변환.
        """
        self.stats.n_chunks += 1
        results = self._run_extractors_parallel(chunk, ctx)
        merged = self._merge_relations(
            [r for ex_result in results for r in ex_result.relations]
        )
        return merged, results

    # ── 병렬 실행 ──────────────────────────────────────────
    def _run_extractors_parallel(
        self, chunk: dict, ctx: RunContext,
    ) -> list[ExtractorResult]:
        results: list[ExtractorResult] = []
        runnable = [e for e in self.extractors if self._circuit_allows(e.name)]
        if len(runnable) < len(self.extractors):
            blocked = len(self.extractors) - len(runnable)
            self.stats.n_circuit_blocks += blocked

        if not runnable:
            return results

        if len(runnable) == 1:
            # 병렬 불필요 — 직접 호출 (BudgetExceeded 그대로 전파)
            ex = runnable[0]
            t0 = time.monotonic()
            try:
                result = ex.safe_extract(chunk, ctx)
            except BudgetExceeded:
                raise
            self._update_circuit(ex.name, result)
            results.append(result)
            self.stats.n_extractor_calls += 1
            self.stats.total_latency_ms += int((time.monotonic() - t0) * 1000)
            self.stats.n_warnings += len(result.warnings)
            return results

        # 다중 → ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            futures = {
                pool.submit(ex.safe_extract, chunk, ctx): ex
                for ex in runnable
            }
            # extractor 마다 다른 timeout 적용. wait 는 전체 ALL_COMPLETED.
            wait(futures, timeout=max(e.timeout_ms for e in runnable) / 1000.0 + 5.0,
                 return_when=ALL_COMPLETED)
            for fut, ex in futures.items():
                try:
                    result = fut.result(timeout=0.1)
                except BudgetExceeded:
                    # cancellation — 다른 future 정리하고 즉시 전파
                    for other in futures:
                        if not other.done():
                            other.cancel()
                    raise
                except Exception as e:
                    result = ExtractorResult.empty(
                        ex.name, ex.version,
                        warnings=(f"future_error: {e}",),
                    )
                self._update_circuit(ex.name, result)
                results.append(result)
                self.stats.n_extractor_calls += 1
                self.stats.total_latency_ms += result.latency_ms
                self.stats.n_warnings += len(result.warnings)

        return results

    # ── circuit breaker ────────────────────────────────────
    def _circuit_allows(self, name: str) -> bool:
        cs = self._circuits.get(name)
        if cs is None:
            return True
        if cs.cooled_until and time.monotonic() < cs.cooled_until:
            return False
        return True

    def _update_circuit(self, name: str, result: ExtractorResult) -> None:
        cs = self._circuits[name]
        # 실패 판정: warnings 중 'exception' 또는 'future_error' 포함.
        failed = any(
            w.startswith(("exception", "future_error")) for w in result.warnings
        )
        if failed:
            cs.fail_streak += 1
            if cs.fail_streak >= MAX_FAIL_STREAK:
                cs.cooled_until = time.monotonic() + COOLDOWN_S
                log.warning(
                    "[engine] %s circuit OPEN — cooldown %.0fs (streak=%d)",
                    name, COOLDOWN_S, cs.fail_streak,
                )
        else:
            cs.fail_streak = 0
            cs.cooled_until = 0.0

    # ── merge / dedupe ─────────────────────────────────────
    @staticmethod
    def _merge_relations(relations: Sequence[dict]) -> list[dict]:
        """같은 (head, relation, tail, fiscal_year) 키는 1개로 합침.

        - confidence: 최댓값
        - evidence: 모든 evidence 텍스트 set 합집합 (중복 제거)
        - extractor 출처 추적: '_extracted_by' 리스트에 누적
        """
        by_key: dict[tuple, dict] = {}
        for rel in relations:
            key = (
                _norm(rel.get("head")),
                rel.get("relation"),
                _norm(rel.get("tail")),
                rel.get("fiscal_year") or rel.get("_fiscal_year"),
            )
            if key in by_key:
                existing = by_key[key]
                # confidence max
                existing["confidence"] = max(
                    float(existing.get("confidence") or 0.0),
                    float(rel.get("confidence") or 0.0),
                )
                # evidence 합집합
                existing_ev = set(existing.get("_evidences") or [])
                if rel.get("evidence"):
                    existing_ev.add(rel["evidence"])
                existing["_evidences"] = list(existing_ev)
                # extractor 출처
                by = set(existing.get("_extracted_by") or [])
                if rel.get("_extracted_by"):
                    by.update(rel["_extracted_by"] if isinstance(rel["_extracted_by"], list)
                              else [rel["_extracted_by"]])
                existing["_extracted_by"] = list(by)
            else:
                merged = dict(rel)
                merged["_evidences"] = [rel["evidence"]] if rel.get("evidence") else []
                merged["_extracted_by"] = (
                    [rel["_extracted_by"]] if isinstance(rel.get("_extracted_by"), str)
                    else list(rel.get("_extracted_by") or [])
                )
                by_key[key] = merged
        return list(by_key.values())

    # ── healthcheck ────────────────────────────────────────
    def healthcheck(self) -> dict[str, bool]:
        return {e.name: bool(e.healthcheck()) for e in self.extractors}


def _norm(s) -> str:
    """merge 키용 정규화 — 공백·소문자."""
    if not s:
        return ""
    return str(s).strip().lower()


__all__ = [
    "ExtractorEngine",
    "EngineRunStats",
    "MAX_FAIL_STREAK",
    "COOLDOWN_S",
]
