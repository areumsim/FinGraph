"""LLM 어댑터 — Provider 추상화.

사용:
    from autonexusgraph.llm import get_llm_client

    client = get_llm_client(role="planner")    # 또는 None (기본 모델)
    resp = client.chat([{"role": "user", "content": "안녕"}])
    print(resp.content, resp.usage.cost_usd)
"""

from .base import LLMClient, LLMError, LLMResponse, TokenUsage, get_llm_client

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "TokenUsage",
    "get_llm_client",
]
