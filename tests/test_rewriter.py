"""rewriter 단위 테스트 — LLM 호출 없이 룰 게이트만 검증."""

from __future__ import annotations

import os

from autonexusgraph.agents.rewriter import rewrite_query


def test_no_history_no_call():
    out, audit = rewrite_query(question="삼성전자 매출은?", history=[])
    assert out == "삼성전자 매출은?"
    assert audit["called"] is False
    assert audit["reason"] == "no_demonstrative_or_history"


def test_no_demonstrative_no_call():
    history = [
        {"role": "user", "content": "삼성전자 자회사는?"},
        {"role": "assistant", "content": "삼성디스플레이, 삼성SDI 등."},
    ]
    out, audit = rewrite_query(question="현대차 자회사는?", history=history)
    assert out == "현대차 자회사는?"
    assert audit["called"] is False


def test_demonstrative_triggers_path(monkeypatch):
    """지시어 + history 면 rewrite 시도. LLM 미설정이면 fail-soft (원본)."""
    # LLM 설정이 없는 환경에서는 LLM 호출 실패 → audit.reason 에 runtime_error / unavailable
    monkeypatch.setenv("FINGRAPH_QUERY_REWRITE_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-INVALID-FOR-TEST")
    history = [
        {"role": "user", "content": "삼성전자 자회사는?"},
        {"role": "assistant", "content": "삼성디스플레이, 삼성SDI 등 (10개)."},
    ]
    out, audit = rewrite_query(question="그 중 매출 1조 이상은?", history=history)
    # 시그널: 게이트는 통과 (지시어 + history 있음). LLM 실패는 fail-soft.
    assert audit["reason"] != "no_demonstrative_or_history"
    # 실패 시 원본 그대로
    if audit["called"] is False:
        assert out == "그 중 매출 1조 이상은?"


def test_env_disabled():
    history = [{"role": "user", "content": "X"}, {"role": "assistant", "content": "Y"}]
    os.environ["FINGRAPH_QUERY_REWRITE_ENABLED"] = "false"
    try:
        out, audit = rewrite_query(question="그 중 가장 큰 곳은?", history=history)
        assert audit["reason"] == "env_disabled"
        assert out == "그 중 가장 큰 곳은?"
    finally:
        os.environ.pop("FINGRAPH_QUERY_REWRITE_ENABLED", None)


def test_short_followup_triggers():
    history = [
        {"role": "user", "content": "삼성전자 자회사는?"},
        {"role": "assistant", "content": "삼성디스플레이 등."},
    ]
    # 짧은 follow-up (10자 미만) — 지시어가 없어도 게이트 통과
    out, audit = rewrite_query(question="매출은?", history=history)
    assert audit["reason"] != "no_demonstrative_or_history"
