"""SEC EDGAR OEM ingestion + loader 단위 검증.

DB / SEC API 모두 mock — JSON 파서·bridge upsert·SQL 시그니처만.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── ingestion 모듈 ─────────────────────────────────────────
def test_ingestion_module_importable():
    from autograph.ingestion import sec_oem as I
    assert hasattr(I, "fetch_company_facts")
    assert hasattr(I, "OEM_SEED")
    # 시드에 핵심 OEM 들 포함.
    ciks = {c for c, *_ in I.OEM_SEED}
    assert 1318605 in ciks    # Tesla
    assert 37996   in ciks    # Ford
    assert 1467858 in ciks    # GM


def test_oem_seed_structure():
    """각 seed row 는 (cik, name, ticker, form, country) 5-tuple."""
    from autograph.ingestion.sec_oem import OEM_SEED
    for row in OEM_SEED:
        assert len(row) == 5
        cik, name, ticker, form, country = row
        assert isinstance(cik, int) and cik > 0
        assert name and ticker and form and country


# ── loader 모듈 ────────────────────────────────────────────
def test_loader_module_importable():
    from autograph.loaders import load_auto_oem_sec as L
    assert callable(L.load_oem_sec)
    assert hasattr(L, "_GAAP_CONCEPTS")
    assert hasattr(L, "_DEI_CONCEPTS")
    # 핵심 회계 항목 — Revenue + NetIncomeLoss + R&D.
    gaap = set(L._GAAP_CONCEPTS)
    for c in ("Revenues", "NetIncomeLoss", "OperatingIncomeLoss",
              "ResearchAndDevelopmentExpense", "Assets",
              "StockholdersEquity"):
        assert c in gaap


# ── _iter_facts — JSON 파싱 ───────────────────────────────
def test_iter_facts_extracts_us_gaap_revenues():
    from autograph.loaders.load_auto_oem_sec import _iter_facts

    facts_root = {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [
                        {"end": "2023-12-31", "val": 96773000000,
                         "accn": "0001628280-24-002390",
                         "fy": 2023, "fp": "FY", "form": "10-K",
                         "filed": "2024-01-29"},
                        {"end": "2022-12-31", "val": 81462000000,
                         "accn": "0000950170-23-001409",
                         "fy": 2022, "fp": "FY", "form": "10-K",
                         "filed": "2023-01-31"},
                    ]
                }
            },
            "NotInWhitelist": {"units": {"USD": [{"val": 999}]}},
        }
    }
    out = list(_iter_facts(facts_root, "us-gaap", ("Revenues", "NetIncomeLoss")))
    assert len(out) == 2
    assert out[0]["concept"] == "Revenues"
    assert out[0]["val"] == 96773000000
    assert out[0]["fy"] == 2023
    assert out[0]["fp"] == "FY"
    assert out[0]["form"] == "10-K"
    # NotInWhitelist 는 빠짐.
    concepts = {f["concept"] for f in out}
    assert "NotInWhitelist" not in concepts


def test_iter_facts_empty_taxonomy():
    from autograph.loaders.load_auto_oem_sec import _iter_facts
    assert list(_iter_facts({}, "us-gaap", ("Revenues",))) == []
    assert list(_iter_facts({"us-gaap": {}}, "us-gaap", ("Revenues",))) == []


def test_iter_facts_dei_concepts():
    from autograph.loaders.load_auto_oem_sec import _iter_facts
    facts_root = {
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        {"end": "2024-01-29", "val": 3185000000,
                         "accn": "x", "fp": "FY", "form": "10-K"},
                    ]
                }
            }
        }
    }
    out = list(_iter_facts(facts_root, "dei",
                            ("EntityCommonStockSharesOutstanding",)))
    assert len(out) == 1
    assert out[0]["unit"] == "shares"
    assert out[0]["val"] == 3185000000


# ── _resolve_manufacturer_id — entity_name 매칭 ───────────
def test_resolve_manufacturer_id_via_bridge_sec_cik():
    """bridge 에 이미 sec_cik 매핑이 있으면 즉시 반환."""
    from autograph.loaders.load_auto_oem_sec import _resolve_manufacturer_id

    cur = MagicMock()
    def fake_execute(sql, params=None):
        if "sec_cik" in sql and "bridge.corp_entity" in sql:
            cur._row = (42,)
        else:
            cur._row = None
    cur.execute = fake_execute
    cur.fetchone = lambda: cur._row

    out = _resolve_manufacturer_id(cur, entity_name="Tesla, Inc.", cik10="0001318605")
    assert out == 42


def test_resolve_manufacturer_id_via_name_exact():
    """bridge 매핑 없고 → entity_name name_norm 정확 매칭."""
    from autograph.loaders.load_auto_oem_sec import _resolve_manufacturer_id

    cur = MagicMock()
    state = {"q": 0}
    def fake_execute(sql, params=None):
        state["q"] += 1
        if state["q"] == 1:
            # bridge sec_cik 매핑 없음.
            cur._row = None
        elif state["q"] == 2:
            # name_norm 매칭 — 'tesla, inc.' 가 PG 에 있음.
            cur._row = (101,)
        else:
            cur._row = None
    cur.execute = fake_execute
    cur.fetchone = lambda: cur._row

    out = _resolve_manufacturer_id(cur, entity_name="Tesla, Inc.", cik10="0001318605")
    assert out == 101


def test_resolve_manufacturer_id_via_suffix_strip():
    """exact 매칭 실패 → ', Inc.' 떼고 매칭 시도."""
    from autograph.loaders.load_auto_oem_sec import _resolve_manufacturer_id

    cur = MagicMock()
    state = {"q": 0}
    matched = {"by_clean_name": False}

    def fake_execute(sql, params=None):
        state["q"] += 1
        if state["q"] == 1:
            cur._row = None        # bridge sec_cik 없음
        elif state["q"] == 2:
            cur._row = None        # 'tesla, inc.' name_norm 없음
        else:
            # suffix-trim 후 'tesla' 매칭.
            param0 = params[0] if params else ""
            if "tesla" in (param0 or "").lower():
                matched["by_clean_name"] = True
                cur._row = (202,)
            else:
                cur._row = None
    cur.execute = fake_execute
    cur.fetchone = lambda: cur._row

    out = _resolve_manufacturer_id(cur, entity_name="Tesla, Inc.", cik10="0001318605")
    assert out == 202
    assert matched["by_clean_name"]


def test_resolve_manufacturer_id_no_match():
    """모든 매칭 실패 → None."""
    from autograph.loaders.load_auto_oem_sec import _resolve_manufacturer_id

    cur = MagicMock()
    cur.execute = lambda *a, **kw: None
    cur.fetchone = lambda: None

    out = _resolve_manufacturer_id(cur, entity_name="Unknown Corp",
                                    cik10="9999999999")
    assert out is None


# ── _process_cik_file — 통합 ───────────────────────────────
def _make_facts_file(tmp_path: Path, cik: str, payload: dict) -> Path:
    p = tmp_path / f"CIK{cik}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_process_cik_file_bridge_and_facts(tmp_path, monkeypatch):
    """매칭 성공 → bridge upsert + facts insert."""
    from autograph.loaders import load_auto_oem_sec as L

    bridge_calls: list = []
    fact_calls: list = []

    cur = MagicMock()
    state = {"step": 0}

    def fake_execute(sql, params=None):
        # 1) bridge sec_cik lookup → 없음.
        # 2) name_norm 매칭 → mfr_id=42.
        # 3+) bridge UPSERT, facts INSERT.
        if "sec_cik = %s" in sql and "bridge.corp_entity" in sql:
            cur._row = None
        elif "FROM auto.master_manufacturers" in sql and "name_norm" in sql:
            cur._row = (42,)
        elif "INSERT INTO bridge.corp_entity" in sql:
            bridge_calls.append(params)
            cur._row = None
        elif "INSERT INTO auto.oem_financials_sec" in sql:
            fact_calls.append(params)
            cur._row = (True,)
    cur.execute = fake_execute
    cur.fetchone = lambda: cur._row

    fpath = _make_facts_file(tmp_path, "0001318605", {
        "cik": 1318605,
        "entityName": "Tesla, Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"end": "2023-12-31", "val": 96773000000,
                             "accn": "X", "fy": 2023, "fp": "FY",
                             "form": "10-K", "filed": "2024-01-29"},
                            {"end": "2022-12-31", "val": 81462000000,
                             "accn": "Y", "fy": 2022, "fp": "FY",
                             "form": "10-K", "filed": "2023-01-31"},
                        ]
                    }
                }
            }
        }
    })

    stats = L.LoadStats()
    L._process_cik_file(cur, fpath, stats)
    assert stats.ciks_seen == 1
    assert stats.ciks_with_facts == 1
    # bridge upsert 1회 (manufacturer_id=42, sec_cik=0001318605, name='Tesla, Inc.').
    assert len(bridge_calls) == 1
    assert bridge_calls[0][0] == "42"           # entity_id
    assert bridge_calls[0][1] == "Tesla, Inc."  # name
    assert bridge_calls[0][2] == "0001318605"   # sec_cik
    # facts insert 2회.
    assert len(fact_calls) == 2
    # 모든 fact 가 manufacturer_id=42 + sec_cik 동일.
    for p in fact_calls:
        assert p[0] == 42
        assert p[1] == "0001318605"
        assert p[2] == "us-gaap"
        assert p[3] == "Revenues"


def test_process_cik_file_unmatched_manufacturer(tmp_path, monkeypatch):
    """manufacturer 매칭 실패 → bridge 안 만듦, facts 도 manufacturer_id=NULL."""
    from autograph.loaders import load_auto_oem_sec as L

    fact_calls: list = []
    cur = MagicMock()

    def fake_execute(sql, params=None):
        # 모든 lookup 실패.
        if "INSERT INTO auto.oem_financials_sec" in sql:
            fact_calls.append(params)
            cur._row = (True,)
        else:
            cur._row = None
    cur.execute = fake_execute
    cur.fetchone = lambda: cur._row

    fpath = _make_facts_file(tmp_path, "0009999999", {
        "cik": 9999999,
        "entityName": "Unknown OEM Corp",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {"USD": [
                        {"end": "2023-12-31", "val": 100,
                         "fy": 2023, "fp": "FY", "form": "10-K"},
                    ]}
                }
            }
        }
    })
    stats = L.LoadStats()
    L._process_cik_file(cur, fpath, stats)
    assert stats.bridge_rows_unmatched == 1
    assert stats.bridge_rows_upserted == 0
    # facts 는 그대로 들어가지만 manufacturer_id 는 NULL.
    assert len(fact_calls) == 1
    assert fact_calls[0][0] is None     # manufacturer_id NULL
    assert fact_calls[0][1] == "0009999999"


def test_process_cik_file_no_facts(tmp_path):
    """facts 비어있음 → with_facts 카운트 안 증가."""
    from autograph.loaders import load_auto_oem_sec as L

    cur = MagicMock()
    cur.execute = lambda *a, **kw: None
    cur.fetchone = lambda: None

    fpath = _make_facts_file(tmp_path, "0001234567", {
        "cik": 1234567, "entityName": "X",
        "facts": {},
    })
    stats = L.LoadStats()
    L._process_cik_file(cur, fpath, stats)
    assert stats.ciks_seen == 1
    assert stats.ciks_with_facts == 0


# ── bridge tool 함수 ──────────────────────────────────────
def test_bridge_tools_exposed():
    from autograph.tools import (
        bridge_sec_cik_to_entity,
        bridge_entity_to_sec_cik,
        get_oem_financials_sec,
    )
    assert callable(bridge_sec_cik_to_entity)
    assert callable(bridge_entity_to_sec_cik)
    assert callable(get_oem_financials_sec)


def test_bridge_sec_cik_pads_to_10_digits(monkeypatch):
    """CIK '1318605' 입력 → '0001318605' 로 zero-pad 후 조회."""
    from autograph.tools import bridge as br

    captured: list = []
    def fake_query(sql, params):
        captured.append(params)
        return []
    monkeypatch.setattr(br, "query_dicts", fake_query)

    br.bridge_sec_cik_to_entity("1318605")
    assert captured[0][0] == "0001318605"


def test_bridge_sec_cik_rejects_invalid_entity_type():
    from autograph.tools import bridge as br
    with pytest.raises(ValueError):
        br.bridge_sec_cik_to_entity("0001318605", entity_type="invalid")


# ── workers / planner 통합 ────────────────────────────────
def test_workers_whitelist_includes_sec_tools():
    from autonexusgraph.agents.workers import _AUTO_SQL_ALLOWED
    assert "bridge_sec_cik_to_entity" in _AUTO_SQL_ALLOWED
    assert "bridge_entity_to_sec_cik" in _AUTO_SQL_ALLOWED
    assert "get_oem_financials_sec" in _AUTO_SQL_ALLOWED


def test_cross_domain_planner_uses_sec_when_models_present():
    """plan_cross_domain_tasks 가 target_models 받으면 SEC OEM task 도 생성."""
    from autograph.policy import plan_cross_domain_tasks

    tasks = plan_cross_domain_tasks(
        question="Tesla 2023 매출과 Model Y 리콜 관계",
        target_companies=[],
        target_models=[55],
    )
    intents = [t["intent"] for t in tasks]
    assert "bridge_entity_to_sec_cik" in intents
    assert "get_oem_financials_sec" in intents
    assert "search_documents_auto" in intents


def test_cross_domain_planner_uses_corp_code_when_companies():
    """target_companies (DART) 있으면 bridge_corp_to_entity + get_revenue."""
    from autograph.policy import plan_cross_domain_tasks

    tasks = plan_cross_domain_tasks(
        question="현대자동차 2024 매출",
        target_companies=["00164742"],
    )
    intents = [t["intent"] for t in tasks]
    assert "bridge_corp_to_entity" in intents
    assert "get_revenue" in intents
