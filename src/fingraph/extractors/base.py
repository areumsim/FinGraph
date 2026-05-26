"""BaseExtractor — P3 관계 추출기의 공통 계약.

설계 메모 (이전 v2/contracts/base_extractor.py 패턴 흡수):
- 순수 함수: 외부 상태 변경 금지. write 는 별도 loader 가 단일화.
- timeout / 예외는 safe_extract wrapper 가 흡수 (partial result + warning).
- ExtractorEngine 이 여러 추출기를 병렬 실행하고 결과를 merge.

우리 도메인 단순화:
- entity 는 P2 정형(DART)에서 SSOT — extractor 는 relation 만 추출.
- chunk 입력 = vec.chunks row dict (id, corp_code, text, fiscal_year, section, ...)
- RunContext = LLM client / 비용 가드 / company name resolver 묶음.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunContext:
    """추출 실행 컨텍스트 — extractor 가 의존하는 외부 자원 묶음.

    LLM client (budget_aware), 회사명 resolver, 프롬프트 SSOT 등.
    """

    llm_client: Any | None = None             # BudgetAwareLLMClient (또는 None — 룰만 쓰는 extractor 용)
    company_name_resolver: dict[str, str] = field(default_factory=dict)
    prompt_spec: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractorResult:
    """단일 추출기의 산출.

    relations: P3 관계 dict 들. schema 는 ontology/relations.yaml + prompts/relation_extract.yaml.
    warnings: timeout / 예외 등 비치명적 경고 (적재는 정상 진행).
    """

    relations: Sequence[dict] = field(default_factory=tuple)
    extractor_name: str = ""
    extractor_version: str = ""
    latency_ms: int = 0
    warnings: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def empty(
        cls,
        name: str,
        version: str,
        *,
        warnings: Sequence[str] = (),
        latency_ms: int = 0,
    ) -> "ExtractorResult":
        return cls(
            relations=(), extractor_name=name, extractor_version=version,
            latency_ms=latency_ms, warnings=tuple(warnings),
        )


class BaseExtractor(ABC):
    """모든 추출기의 공통 계약.

    구현체:
    - LLMRelationExtractor   : 사업보고서 본문 → LLM JSON 추출 (P3)
    - (향후) RegexExtractor   : 정규식 패턴 매칭 (룰 — LLM 0 비용)
    - (향후) LexiconExtractor : 사전 기반 (예: 산업분류 어휘)
    """

    name: str = "base"
    version: str = "0.0.0"
    timeout_ms: int = 30000
    deterministic: bool = True              # False = LLM extractor

    @abstractmethod
    def extract(self, chunk: dict, ctx: RunContext) -> ExtractorResult:
        """순수함수. 외부 부수효과 금지. timeout 초과 시 empty + warning."""

    def healthcheck(self) -> bool:
        """엔진 시작 시 호출 가능 — 외부 의존 (LLM/DB) 검증."""
        return True

    def safe_extract(self, chunk: dict, ctx: RunContext) -> ExtractorResult:
        """timeout/예외를 흡수한 wrapper — engine 이 호출.

        BudgetExceeded 만은 re-raise (배치 abort 신호).
        """
        from ..llm.cost_tracker import BudgetExceeded

        t0 = time.monotonic()
        try:
            return self.extract(chunk, ctx)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            elapsed = int((time.monotonic() - t0) * 1000)
            return ExtractorResult.empty(
                self.name, self.version,
                warnings=(f"exception: {type(exc).__name__}: {exc} (latency_ms={elapsed})",),
                latency_ms=elapsed,
            )


__all__ = ["BaseExtractor", "ExtractorResult", "RunContext"]
