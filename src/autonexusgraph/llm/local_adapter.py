"""로컬 LLM 어댑터 — vLLM, Ollama, 또는 OpenAI-호환 self-hosted.

LLM_PROVIDER=local 일 때 사용.
LOCAL_LLM_BASE_URL 에 OpenAI 호환 endpoint 를 지정 (기본 http://localhost:8000/v1).

대부분의 self-hosted 서버 (vLLM, llama.cpp, Ollama with openai-compat,
TGI, LMDeploy) 가 OpenAI Chat Completions 형식을 지원하므로
openai SDK 를 base_url 만 바꿔 재사용.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from .base import LLMClient, LLMError, LLMResponse, TokenUsage


class LocalLLMClient(LLMClient):
    """OpenAI-compatible local endpoint wrapper."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str = "EMPTY",
        timeout: float = 120.0,
    ) -> None:
        from openai import OpenAI  # 호환 client 재사용

        self.model = model
        self.base_url = base_url
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

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
            raise LLMError(f"Local LLM chat failed ({self.base_url}): {e}") from e
        usage = _usage_from(resp.usage, self.model)
        return LLMResponse(content=resp.choices[0].message.content or "", usage=usage, raw=resp)

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
            raise LLMError(f"Local LLM stream failed: {e}") from e
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
        """로컬 LLM JSON: 우선 response_format=json_object, 실패 시 프롬프트 + 파싱.

        주의: 로컬 모델은 strict json_schema 미지원이 많음. response_format=json_object
        만 지원하거나 그것도 안 되면 system message 에 schema 박고 자유 파싱.
        """
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                response_format={"type": "json_object"},
                **kwargs,
            )
        except Exception:
            # 폴백: schema 를 system 에 주입 후 자유 생성
            schema_str = json.dumps(schema.get("schema", schema), ensure_ascii=False, indent=2)
            augmented = [
                {"role": "system",
                 "content": f"Respond ONLY with valid JSON matching this schema:\n{schema_str}"},
                *messages,
            ]
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=augmented,  # type: ignore[arg-type]
                    temperature=temperature,
                    **kwargs,
                )
            except Exception as e:
                raise LLMError(f"Local LLM json failed: {e}") from e
        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"Local LLM returned invalid JSON: {content[:200]}") from e


def _usage_from(usage: Any, model: str) -> TokenUsage:
    if usage is None:
        return TokenUsage(model=model)
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=0.0,        # 로컬은 비용 0
        model=model,
    )
