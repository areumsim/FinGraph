"""P3 관계 추출기 — schema-aware + signal filter + 다단 병렬.

핵심 모듈:
- base.py            : BaseExtractor / ExtractorResult / RunContext (계약)
- _selective.py      : signal-based 청크 사전 필터 (LLM 호출 50%+ 절감)
- llm_relations.py   : LLM 호출 함수 (extract_one / filter_target_chunks)
- llm_extractor.py   : LLMRelationExtractor — BaseExtractor 구현체
- engine.py          : ExtractorEngine — 병렬 + circuit breaker + dedupe merge
- validator.py       : P4 cross-validate (P3 산출 vs P2 정형 데이터)
"""

from .base import BaseExtractor, ExtractorResult, RunContext
from .engine import ExtractorEngine, EngineRunStats
from .llm_extractor import LLMRelationExtractor
from .validator import validate_relations, ValidationResult


__all__ = [
    "BaseExtractor", "ExtractorResult", "RunContext",
    "ExtractorEngine", "EngineRunStats",
    "LLMRelationExtractor",
    "validate_relations", "ValidationResult",
]
