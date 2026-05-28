"""OpenAI 어댑터 — gpt-4o, gpt-4o-mini 등.

LLM_PROVIDER=openai 일 때 사용.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from .base import LLMClient, LLMError, LLMResponse, TokenUsage


# 모델별 토큰 단가 (USD, 1M 토큰당) — 2025-01 기준 공식 가격
# 갱신: https://openai.com/api/pricing/
_PRICING: dict[str, tuple[float, float]] = {
    # model_id : (input_per_1m, output_per_1m)
    "gpt-4o":              (2.50, 10.00),
    "gpt-4o-mini":         (0.15,  0.60),
    "gpt-4o-2024-11-20":   (2.50, 10.00),
    "gpt-4-turbo":         (10.0, 30.00),
    "o1":                  (15.0, 60.00),
    "o1-mini":             (3.00, 12.00),
}


class OpenAIClient(LLMClient):
    """OpenAI Chat Completions API wrapper."""

    def __init__(self, model: str, api_key: str, timeout: float = 120.0) -> None:
        if not api_key:
            raise LLMError("OPENAI api key 미설정 (.env: LLM_API_KEY)")
        from openai import OpenAI  # lazy import

        self.model = model
        self._client = OpenAI(api_key=api_key, timeout=timeout)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception as e:
            raise LLMError(f"OpenAI chat failed: {e}") from e

        usage = _build_usage(self.model, resp.usage)
        content = resp.choices[0].message.content or ""
        return LLMResponse(content=content, usage=usage, raw=resp)

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs,
            )
        except Exception as e:
            raise LLMError(f"OpenAI stream failed: {e}") from e
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Structured Outputs (response_format=json_schema)."""
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.get("name", "Response"),
                        "schema": schema.get("schema", schema),
                        "strict": True,
                    },
                },
                **kwargs,
            )
        except Exception as e:
            raise LLMError(f"OpenAI json failed: {e}") from e

        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"OpenAI returned invalid JSON: {content[:200]}") from e


def _build_usage(model: str, usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage(model=model)
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    total = getattr(usage, "total_tokens", prompt + completion) or (prompt + completion)
    in_per_1m, out_per_1m = _PRICING.get(model, (0.0, 0.0))
    cost = (prompt * in_per_1m + completion * out_per_1m) / 1_000_000
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cost_usd=cost,
        model=model,
    )
