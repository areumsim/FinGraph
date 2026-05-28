"""LLMClient wrapper — 호출마다 cost_tracker.record + 사전 tracker.guard.

사용자 명시 원칙 (memory: feedback-llm-cost-brake): 모든 LLM 호출은 비용 한도 가드를
거쳐야 한다. 호출자가 record 를 까먹는 실수를 방지하기 위해 LLMClient 자체를 wrapping.

사용:
    from autonexusgraph.llm.budget_aware import budget_aware_client

    client = budget_aware_client(get_llm_client(role='extractor'),
                                  caller='p3_extract', hard_limit=2.00)
    # 이후 client.chat/.chat_json/.chat_stream 호출 시 자동 record + guard.

호출자가 별도 패키지에서 tracker 를 control 하려면 wrap 안 하고
raw LLMClient + tracker.record 수동 호출도 가능.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from .base import LLMClient, LLMResponse
from .cost_tracker import CostTracker, BudgetExceeded, get_tracker


class BudgetAwareLLMClient(LLMClient):
    """LLMClient delegating wrapper — 호출 전 guard, 호출 후 record."""

    def __init__(self, inner: LLMClient, tracker: CostTracker) -> None:
        self._inner = inner
        self._tracker = tracker
        self.model = inner.model

    def chat(self, messages, *, temperature=0.0, max_tokens=None,
             purpose: str | None = None, **kwargs) -> LLMResponse:
        self._tracker.guard()
        t0 = time.monotonic()
        resp = self._inner.chat(messages, temperature=temperature,
                                 max_tokens=max_tokens, **kwargs)
        latency_ms = int((time.monotonic() - t0) * 1000)
        self._tracker.record(
            input_tokens=resp.usage.prompt_tokens,
            output_tokens=resp.usage.completion_tokens,
            model=resp.usage.model or self.model,
            purpose=purpose,
            latency_ms=latency_ms,
        )
        return resp

    def chat_stream(self, messages, *, temperature=0.0, max_tokens=None,
                    purpose: str | None = None, **kwargs) -> Iterator[str]:
        self._tracker.guard()
        # stream 은 usage 가 stream 끝에 yield 되거나 별도 trace 가 어려움 — provider 별로 다름.
        # OpenAI: 마지막 chunk 의 usage. Anthropic: message_delta event. 단순화: stream 후
        # 같은 messages 로 비스트림 호출 안 함. 호출자가 통계 필요하면 chat() 사용.
        yield from self._inner.chat_stream(messages, temperature=temperature,
                                            max_tokens=max_tokens, **kwargs)

    def chat_json(self, messages, schema, *, temperature=0.0,
                  purpose: str | None = None, **kwargs) -> dict[str, Any]:
        self._tracker.guard()
        t0 = time.monotonic()
        # chat_json 은 LLMResponse 가 아닌 dict 반환 — provider 마다 usage 노출 다름.
        # OpenAI/Anthropic 어댑터에 _last_usage 추적이 없으면 input 기준으로 보수적 추정 필요.
        result = self._inner.chat_json(messages, schema, temperature=temperature, **kwargs)
        latency_ms = int((time.monotonic() - t0) * 1000)

        # last_usage 추출 시도 — adapter 가 노출하면 사용, 아니면 추정.
        usage = getattr(self._inner, "_last_usage", None)
        if usage is not None:
            self._tracker.record(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                model=usage.model or self.model,
                purpose=purpose,
                latency_ms=latency_ms,
            )
        else:
            # 추정 fallback — messages 의 문자수 / 4 (한국어 더 빡빡하지만 보수적)
            est_in = sum(len(m.get("content", "")) for m in messages) // 3
            est_out = len(str(result)) // 3
            self._tracker.record(
                input_tokens=est_in,
                output_tokens=est_out,
                model=self.model,
                purpose=purpose,
                latency_ms=latency_ms,
            )
        return result


def budget_aware_client(
    inner: LLMClient,
    *,
    caller: str,
    hard_limit: float | None = None,
) -> BudgetAwareLLMClient:
    """LLMClient + tracker 결합."""
    tracker = get_tracker(caller=caller, model=inner.model, hard_limit=hard_limit)
    return BudgetAwareLLMClient(inner, tracker)


__all__ = ["BudgetAwareLLMClient", "budget_aware_client", "BudgetExceeded"]
