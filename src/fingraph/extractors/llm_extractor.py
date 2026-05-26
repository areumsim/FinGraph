"""LLMRelationExtractor — llm_relations.extract_one 을 BaseExtractor 인터페이스로 wrap.

ExtractorEngine 에 등록 가능. 향후 RegexExtractor / LexiconExtractor 와 병렬 실행하면
LLM 호출 수가 더 줄어든다 (룰이 먼저 잡으면 LLM 안 부름).

지금은 LLM 단독 — 룰 추출기는 후속 PR.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from .base import BaseExtractor, ExtractorResult, RunContext
from .llm_relations import extract_one


class LLMRelationExtractor(BaseExtractor):
    """사업보고서 본문 → LLM JSON 추출 (PARTNER_OF/COMPETES_WITH/INVESTED_IN/PRODUCES)."""

    name = "llm_relations_v1"
    version = "0.1"
    timeout_ms = 60000
    deterministic = False

    def extract(self, chunk: dict, ctx: RunContext) -> ExtractorResult:
        if ctx.llm_client is None:
            return ExtractorResult.empty(
                self.name, self.version,
                warnings=("ctx.llm_client is None — LLM extractor 가 client 필요",),
            )
        t0 = time.monotonic()
        result = extract_one(
            chunk,
            company_name_resolver=ctx.company_name_resolver,
            client=ctx.llm_client,
            prompt=ctx.prompt_spec,
            purpose="p3_extract_engine",
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        if result is None:
            return ExtractorResult.empty(
                self.name, self.version,
                warnings=("LLM 호출 실패",), latency_ms=elapsed,
            )
        # extract_one 결과 → relation dict 들에 출처 표시 후 반환
        relations = [
            {
                **rel,
                "_extracted_by": self.name,
                "_fiscal_year": result.fiscal_year,
                "_chunk_id": result.chunk_id,
                "_corp_code": result.corp_code,
            }
            for rel in (result.relations or [])
        ]
        return ExtractorResult(
            relations=relations,
            extractor_name=self.name,
            extractor_version=self.version,
            latency_ms=elapsed,
        )

    def healthcheck(self) -> bool:
        return True   # ctx 단계에서 client 확인 — 여기선 항상 True


__all__ = ["LLMRelationExtractor"]
