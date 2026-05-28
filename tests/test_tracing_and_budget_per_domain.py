"""P5 (autograph tracing tags/metadata) + P6 (per-domain turn budget) 검증."""

from __future__ import annotations

import pytest


# ── P5 — tracing.tags_for_domain / metadata_for_state ─────
def test_tags_for_domain_finance():
    from autonexusgraph.agents.tracing import tags_for_domain
    tags = tags_for_domain("finance")
    assert "domain:finance" in tags
    assert "autonexusgraph" in tags
    assert "autograph" not in tags


def test_tags_for_domain_auto():
    from autonexusgraph.agents.tracing import tags_for_domain
    tags = tags_for_domain("auto")
    assert "domain:auto" in tags
    assert "autograph" in tags


def test_tags_for_domain_cross_domain():
    from autonexusgraph.agents.tracing import tags_for_domain
    tags = tags_for_domain("cross_domain")
    assert "domain:cross_domain" in tags
    assert "autograph" in tags


def test_tags_for_domain_default():
    """None / 빈문자 → finance 로 폴백."""
    from autonexusgraph.agents.tracing import tags_for_domain
    assert "domain:finance" in tags_for_domain(None)
    assert "domain:finance" in tags_for_domain("")
    assert "domain:finance" in tags_for_domain("   ")


def test_metadata_for_state_extracts_counts():
    from autonexusgraph.agents.tracing import metadata_for_state
    md = metadata_for_state({
        "domain": "auto",
        "question_kind": "factual",
        "target_companies": ["00126380"],
        "target_vehicles": [1, 2, 3],
        "target_models": [10],
        "history": [{"role": "user", "content": "x"},
                    {"role": "assistant", "content": "y"}],
    })
    assert md["domain"] == "auto"
    assert md["question_kind"] == "factual"
    assert md["n_target_vehicles"] == 3
    assert md["n_target_models"] == 1
    assert md["n_target_companies"] == 1
    assert md["n_history"] == 2


def test_metadata_for_state_no_pii_leak():
    """PII / 실제 corp_code · variant_id 는 metadata 에 노출되지 않아야."""
    from autonexusgraph.agents.tracing import metadata_for_state
    md = metadata_for_state({
        "domain": "finance",
        "target_companies": ["00126380", "00164742"],
        "target_vehicles": [42],
    })
    flat = str(md)
    assert "00126380" not in flat
    assert "42" not in flat or "n_" in flat   # 길이만 노출


def test_metadata_for_state_invalid_input():
    from autonexusgraph.agents.tracing import metadata_for_state
    assert metadata_for_state(None)["domain"] == "finance"   # type: ignore[arg-type]
    assert metadata_for_state("garbage")["domain"] == "finance"   # type: ignore[arg-type]


# ── _make_run_config — state 전달 시 tags/metadata 부착 ────
def test_make_run_config_includes_domain_tags(monkeypatch):
    from autonexusgraph.agents import graph as G

    # tracing callbacks 는 비활성 (확인은 tags/metadata 만).
    monkeypatch.setattr("autonexusgraph.agents.tracing.get_trace_callbacks",
                         lambda: [])
    cfg = G._make_run_config("tid-1", state={
        "domain": "auto",
        "target_vehicles": [1, 2],
    })
    assert cfg["configurable"]["thread_id"] == "tid-1"
    assert "autograph" in cfg["tags"]
    assert "domain:auto" in cfg["tags"]
    assert cfg["metadata"]["domain"] == "auto"
    assert cfg["metadata"]["n_target_vehicles"] == 2


def test_make_run_config_without_state_omits_tags(monkeypatch):
    from autonexusgraph.agents import graph as G
    monkeypatch.setattr("autonexusgraph.agents.tracing.get_trace_callbacks",
                         lambda: [])
    cfg = G._make_run_config("tid-2")
    assert "tags" not in cfg
    assert "metadata" not in cfg


# ── P6 — turn_budget_for_domain 분기 ──────────────────────
def test_turn_budget_finance_uses_default(monkeypatch):
    from autonexusgraph.config import get_settings, turn_budget_for_domain

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TURN_BUDGET_USD", "0.30")
    monkeypatch.setenv("AGENT_TURN_BUDGET_AUTO_USD", "0.00")
    get_settings.cache_clear()

    assert turn_budget_for_domain("finance") == pytest.approx(0.30)
    assert turn_budget_for_domain(None) == pytest.approx(0.30)
    # auto override = 0 → 기본값 상속.
    assert turn_budget_for_domain("auto") == pytest.approx(0.30)


def test_turn_budget_auto_override(monkeypatch):
    from autonexusgraph.config import get_settings, turn_budget_for_domain

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TURN_BUDGET_USD", "0.20")
    monkeypatch.setenv("AGENT_TURN_BUDGET_AUTO_USD", "0.50")
    monkeypatch.setenv("AGENT_TURN_BUDGET_CROSS_DOMAIN_USD", "0.80")
    get_settings.cache_clear()

    assert turn_budget_for_domain("finance") == pytest.approx(0.20)
    assert turn_budget_for_domain("auto") == pytest.approx(0.50)
    assert turn_budget_for_domain("cross_domain") == pytest.approx(0.80)


def test_turn_budget_remaining_uses_domain(monkeypatch):
    from autonexusgraph.config import get_settings
    from autonexusgraph.agents.policy import (
        turn_budget_remaining,
        turn_budget_exceeded,
    )

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TURN_BUDGET_USD", "0.10")
    monkeypatch.setenv("AGENT_TURN_BUDGET_AUTO_USD", "1.00")
    get_settings.cache_clear()

    s_fin = {"domain": "finance", "llm_usage_usd": 0.08}
    s_auto = {"domain": "auto", "llm_usage_usd": 0.08}

    # finance: 0.10 - 0.08 = 0.02 남음 (남음).
    assert turn_budget_remaining(s_fin) == pytest.approx(0.02)
    assert not turn_budget_exceeded(s_fin)

    # auto: 1.00 - 0.08 = 0.92 남음 (훨씬 여유).
    assert turn_budget_remaining(s_auto) == pytest.approx(0.92)
    assert not turn_budget_exceeded(s_auto)

    # finance turn 이 누적해서 0.15 됐다 → 초과.
    s_fin_over = {"domain": "finance", "llm_usage_usd": 0.15}
    assert turn_budget_exceeded(s_fin_over)
    # auto 는 같은 누적이어도 여유.
    s_auto_over = {"domain": "auto", "llm_usage_usd": 0.15}
    assert not turn_budget_exceeded(s_auto_over)
