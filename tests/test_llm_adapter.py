"""LLM 어댑터 unit 테스트 — 외부 호출 mock."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from autonexusgraph.llm.base import LLMClient, LLMError, TokenUsage, get_llm_client


def test_token_usage_addition():
    a = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.01, model="m")
    b = TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30, cost_usd=0.02, model="m")
    c = a + b
    assert c.prompt_tokens == 30
    assert c.completion_tokens == 15
    assert c.total_tokens == 45
    assert c.cost_usd == pytest.approx(0.03)


def test_llm_client_is_abstract():
    with pytest.raises(TypeError):
        LLMClient()  # type: ignore[abstract]


def test_get_llm_client_role_mapping(monkeypatch):
    """role 인자가 settings.llm_model_<role> 로 매핑되는지."""
    from autonexusgraph.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("LLM_MODEL_PLANNER", "claude-sonnet-4-5")

    # role=planner → claude-sonnet-4-5 로 매핑, 그러나 provider=openai 라 OpenAIClient 가 생성됨
    # (factory 가 model 만 결정하고 provider 별 구현체로 보냄)
    with patch("openai.OpenAI") as mock_openai:
        client = get_llm_client(role="planner")
        assert client.model == "claude-sonnet-4-5"
        mock_openai.assert_called_once_with(api_key="sk-test", timeout=120.0)


def test_get_llm_client_default_model(monkeypatch):
    from autonexusgraph.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")

    with patch("openai.OpenAI"):
        client = get_llm_client()
        assert client.model == "gpt-4o-mini"


def test_get_llm_client_unknown_provider(monkeypatch):
    from autonexusgraph.config import get_settings

    get_settings.cache_clear()
    # Settings 가 Literal validator 로 막아서 ValidationError. 우리 LLMError 도달 X.
    monkeypatch.setenv("LLM_PROVIDER", "xxx")
    with pytest.raises(Exception):
        get_llm_client()


def test_openai_chat_returns_llm_response(monkeypatch):
    from autonexusgraph.llm.openai_adapter import OpenAIClient

    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content="hi"))]
    fake_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=2, total_tokens=12)

    with patch("openai.OpenAI") as mock_openai:
        instance = MagicMock()
        instance.chat.completions.create.return_value = fake_resp
        mock_openai.return_value = instance

        client = OpenAIClient(model="gpt-4o-mini", api_key="sk-test")
        resp = client.chat([{"role": "user", "content": "hello"}])

        assert resp.content == "hi"
        assert resp.usage.prompt_tokens == 10
        assert resp.usage.completion_tokens == 2
        assert resp.usage.cost_usd > 0  # gpt-4o-mini 단가 매칭


def test_anthropic_split_messages():
    """system 메시지를 분리해서 system 인자로 보내는지."""
    from autonexusgraph.llm.anthropic_adapter import AnthropicClient

    with patch("anthropic.Anthropic"):
        client = AnthropicClient(model="claude-sonnet-4-5", api_key="sk-ant-test")
        system, rest = client._split_messages([
            {"role": "system", "content": "you are X"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "also Y"},
            {"role": "assistant", "content": "ok"},
        ])
        assert system == "you are X\n\nalso Y"
        assert rest == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
        ]


def test_openai_missing_key_raises(monkeypatch):
    from autonexusgraph.llm.openai_adapter import OpenAIClient

    with pytest.raises(LLMError, match="api key"):
        OpenAIClient(model="gpt-4o", api_key="")
