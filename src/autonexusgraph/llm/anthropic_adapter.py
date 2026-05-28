"""Anthropic 어댑터 — Claude Sonnet/Opus/Haiku 등.

LLM_PROVIDER=anthropic 일 때 사용.

JSON 구조화 출력은 tool_use 패턴 사용 (Anthropic 권장):
schema 를 가상의 tool 의 input_schema 로 등록하고, tool_choice 로 강제.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .base import LLMClient, LLMError, LLMResponse, TokenUsage


# 모델별 토큰 단가 (USD, 1M 토큰당)
# https://www.anthropic.com/pricing
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":          (15.00, 75.00),
    "claude-opus-4-5":          (15.00, 75.00),
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-sonnet-4-5":         (3.00, 15.00),
    "claude-3-5-sonnet-20241022":(3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80,  4.00),
    "claude-haiku-4-5-20251001": (0.80,  4.00),
}


class AnthropicClient(LLMClient):
    """Anthropic Messages API wrapper."""

    def __init__(self, model: str, api_key: str, timeout: float = 120.0) -> None:
        if not api_key:
            raise LLMError("Anthropic api key 미설정 (.env: LLM_API_KEY)")
        from anthropic import Anthropic  # lazy import

        self.model = model
        self._client = Anthropic(api_key=api_key, timeout=timeout)

    def _split_messages(
        self, messages: list[dict[str, str]]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Anthropic 은 system 을 별도 인자로 받는다. 첫 system role 만 추출."""
        system_parts: list[str] = []
        rest: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system":
                system_parts.append(m.get("content", ""))
            else:
                rest.append({"role": m["role"], "content": m["content"]})
        system = "\n\n".join(p for p in system_parts if p) or None
        return system, rest

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        system, msgs = self._split_messages(messages)
        try:
            resp = self._client.messages.create(
                model=self.model,
                system=system if system is not None else "",
                messages=msgs,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens or 4096,
                **kwargs,
            )
        except Exception as e:
            raise LLMError(f"Anthropic chat failed: {e}") from e

        # content blocks 중 text 만 합침
        text_parts = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))
        content = "".join(text_parts)
        usage = _build_usage(self.model, resp.usage)
        return LLMResponse(content=content, usage=usage, raw=resp)

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        system, msgs = self._split_messages(messages)
        try:
            with self._client.messages.stream(
                model=self.model,
                system=system if system is not None else "",
                messages=msgs,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens or 4096,
                **kwargs,
            ) as stream:
                for text in stream.text_stream:
                    yield text
        except Exception as e:
            raise LLMError(f"Anthropic stream failed: {e}") from e

    def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Anthropic JSON: tool_use 강제. schema 를 tool input_schema 로 사용."""
        system, msgs = self._split_messages(messages)
        tool_name = schema.get("name", "structured_response")
        input_schema = schema.get("schema", schema)
        try:
            resp = self._client.messages.create(
                model=self.model,
                system=system if system is not None else "",
                messages=msgs,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=kwargs.pop("max_tokens", 4096),
                tools=[{
                    "name": tool_name,
                    "description": schema.get("description", "Return structured response"),
                    "input_schema": input_schema,
                }],
                tool_choice={"type": "tool", "name": tool_name},
                **kwargs,
            )
        except Exception as e:
            raise LLMError(f"Anthropic json failed: {e}") from e

        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
                return dict(getattr(block, "input", {}))
        raise LLMError("Anthropic did not return tool_use block")


def _build_usage(model: str, usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage(model=model)
    prompt = getattr(usage, "input_tokens", 0) or 0
    completion = getattr(usage, "output_tokens", 0) or 0
    in_per_1m, out_per_1m = _PRICING.get(model, (0.0, 0.0))
    cost = (prompt * in_per_1m + completion * out_per_1m) / 1_000_000
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=cost,
        model=model,
    )
