"""평가 어댑터 SSOT — AgentAdapter / AgentResponse / Evidence.

여러 시스템(vector-only / graph-only / hybrid / sql+vec) 의 이질적 응답을 평가
레이어가 사용할 단일 형태로 정규화. metric / runner 는 이 dataclass 만 의존.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Evidence:
    """단일 근거 청크.

    FinGraph 시스템은 vec.chunks row 1개를 1 evidence 로 매핑.
    """

    rank: int = 0
    chunk_id: int = 0
    corp_code: str = ""
    rcept_no: str = ""
    section: str = ""
    fiscal_year: int | None = None
    source: str = ""              # 'dart' / 'wikipedia' / ...
    evidence_text: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class AgentResponse:
    """평가 레이어 단일 응답 형태. dataclass.asdict 로 jsonl 직렬화."""

    # 1차 답변
    answer: str = ""
    # refused = 의미 있는 답을 못 줬다 (no_answer / grounding 실패 / 에러)
    # partial 또는 confidence 낮음은 refused 가 아님.
    refused: bool = False
    refusal_reason: str = ""

    # Hits@k 산정용 — pred entity 이름 리스트 (rank 순)
    answer_entities: list[str] = field(default_factory=list)

    # 정규화된 근거
    evidence: list[Evidence] = field(default_factory=list)

    # Cypher — graph/hybrid 어댑터만 채움 (vector-only 는 None)
    cypher: str | None = None

    # 메타
    question_kind: str = ""
    answer_confidence: float | None = None
    data_completeness: str = ""    # complete / partial / insufficient

    # 비용·성능
    latency_sec: float = 0.0
    cost_usd: float = 0.0          # LLM 비용 누적 (sub-agent / planner / synthesizer 합)
    tokens_used: int = 0           # input + output 합

    # raw payload (predictions.jsonl 디버그용)
    raw: dict[str, Any] = field(default_factory=dict)

    # 진단 — failure_mode / aborted_reason / tool 호출 카운트 등
    diagnostics: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(ABC):
    """모든 시스템 어댑터의 공통 ABC.

    name 은 metric report 컬럼명. version 은 회귀 비교용.
    """

    name: str = ""
    version: str = ""

    @abstractmethod
    def query(self, question: str, *,
              domain: str | None = None) -> AgentResponse:
        """질문 → 정규화된 AgentResponse. latency / cost 는 어댑터가 측정.

        ``domain`` 은 gold record 의 'domain' 필드를 그대로 전달 — auto/finance/cross_domain
        라우팅 힌트로만 사용. None 이면 어댑터(혹은 그 하부 에이전트)가 자동 판정.
        """
        raise NotImplementedError
