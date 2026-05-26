"""LLM 어댑터 추상 인터페이스.

모든 Provider(OpenAI/Anthropic/로컬)는 이 인터페이스를 구현한다.
비즈니스 로직은 이 인터페이스만 알면 되고, LLM 종류는 환경변수 LLM_PROVIDER 로 결정된다.

구현체는 후속 PR (Phase 1) 에서 추가:
- openai_adapter.OpenAIClient
- anthropic_adapter.AnthropicClient
- local_adapter.LocalLLMClient

PRD §5 (LLM 추상화 전략) 참조.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenUsage:
    """토큰 사용량 — 비용 추적·평가용."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            model=self.model or other.model,
        )


@dataclass
class LLMResponse:
    """동기 응답 표준 형식."""

    content: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: Any = None  # provider-native response (디버깅용)


class LLMError(Exception):
    """LLM 호출 실패 (timeout, rate limit, invalid response 등)."""


class LLMClient(ABC):
    """모든 LLM Provider 의 공통 인터페이스.

    구현체는 다음 3가지 메서드를 반드시 제공한다:
    - chat: 단일 응답
    - chat_stream: 토큰 스트리밍
    - chat_json: 구조화 출력 (JSON Schema 강제)
    """

    model: str

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """일반 채팅 — 단일 응답 반환."""

    @abstractmethod
    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """스트리밍 — 토큰 단위 yield."""

    @abstractmethod
    def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """구조화 출력 — JSON Schema 강제, dict 반환.

        Planner/Triage/Validator 등 의사결정 노드에서 사용.
        """


def get_llm_client(role: str | None = None) -> LLMClient:
    """팩토리 — 환경변수에 따라 적절한 구현체 반환.

    Args:
        role: 용도별 모델 매핑 (triage/planner/.../judge). None 이면 기본 LLM_MODEL.

    구현은 후속 PR. 지금은 NotImplementedError.
    """
    raise NotImplementedError(
        "LLM 어댑터 구현체는 Phase 1 후속 PR 에서 추가. "
        "예정: OpenAIClient, AnthropicClient, LocalLLMClient"
    )
