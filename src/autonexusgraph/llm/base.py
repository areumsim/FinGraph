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
        """집계 — 동일 모델만 합산을 권장. 다른 모델 혼합 시 model='mixed'.

        cost tracking 의 model 별 정확도를 위해 호출자가 가능하면 모델별로
        TokenUsage 를 분리해서 집계할 것. 본 메서드는 안전한 default 만 제공.
        """
        if not self.model:
            merged_model = other.model
        elif not other.model or self.model == other.model:
            merged_model = self.model
        else:
            merged_model = "mixed"
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            model=merged_model,
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
    """팩토리 — 환경변수에 따라 적절한 LLMClient 구현체 반환.

    Args:
        role: 용도별 모델 매핑 키. 가능한 값:
              triage | planner | supervisor | research | graph | sql |
              calculator | validator | synthesizer | judge | None(기본)
              매핑은 settings.llm_model_<role> 환경변수에서 결정.

    Returns:
        Provider 별 구현체 (OpenAIClient / AnthropicClient / LocalLLMClient).
        설정은 settings.llm_provider 에 따름.
    """
    # 지연 import — 순환 참조 방지 + 미설치 패키지 호환
    from ..config import get_settings

    s = get_settings()
    model = _resolve_model(s, role)

    if s.llm_provider == "openai":
        from .openai_adapter import OpenAIClient
        return OpenAIClient(model=model, api_key=s.llm_api_key, timeout=s.llm_timeout)
    if s.llm_provider == "anthropic":
        from .anthropic_adapter import AnthropicClient
        return AnthropicClient(model=model, api_key=s.llm_api_key, timeout=s.llm_timeout)
    if s.llm_provider == "local":
        from .local_adapter import LocalLLMClient
        return LocalLLMClient(
            model=model,
            base_url=s.local_llm_base_url,
            api_key=s.llm_api_key or "EMPTY",
            timeout=s.llm_timeout,
        )
    raise LLMError(f"unknown LLM_PROVIDER: {s.llm_provider}")


def _resolve_model(settings: Any, role: str | None) -> str:
    """역할별 모델 override 결정."""
    if role:
        attr = f"llm_model_{role}"
        v = getattr(settings, attr, None)
        if v:
            return v
    return settings.llm_model
