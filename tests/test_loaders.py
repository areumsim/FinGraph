"""로더 unit 테스트 — parse·매핑·SQL 생성 검증 (PG 미연결)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── _common ─────────────────────────────────────────────────────────
def test_parse_amount():
    from fingraph.loaders._common import parse_amount

    assert parse_amount("1234567") == 1234567
    assert parse_amount("1,234,567") == 1234567
    assert parse_amount("455,905,980,000,000") == 455_905_980_000_000
    assert parse_amount("(1000)") == -1000        # 회계 음수 표기
    assert parse_amount("-") is None
    assert parse_amount("") is None
    assert parse_amount(None) is None
    assert parse_amount("invalid") is None


def test_parse_date():
    from fingraph.loaders._common import parse_date

    assert parse_date("19690113") == "1969-01-13"
    assert parse_date("20231231") == "2023-12-31"
    assert parse_date("") is None
    assert parse_date(None) is None
    # 이미 -로 구분된 형태는 그대로
    assert parse_date("2023-12-31") == "2023-12-31"


def test_iter_jsonl_missing(tmp_path):
    from fingraph.loaders._common import iter_jsonl

    p = tmp_path / "missing.jsonl"
    assert list(iter_jsonl(p)) == []


def test_iter_jsonl_skips_bad_lines(tmp_path):
    from fingraph.loaders._common import iter_jsonl

    p = tmp_path / "mixed.jsonl"
    p.write_text('{"a":1}\n\nnot json\n{"b":2}\n', encoding="utf-8")
    rows = list(iter_jsonl(p))
    assert rows == [{"a": 1}, {"b": 2}]


def test_chunked():
    from fingraph.loaders._common import chunked

    out = list(chunked(iter(range(7)), 3))
    assert out == [[0, 1, 2], [3, 4, 5], [6]]


# ── companies ────────────────────────────────────────────────────────
def test_companies_build_row(tmp_path):
    from fingraph.loaders.companies import _build_row

    target = {
        "corp_code": "00126380",
        "stock_code": "005930",
        "name_dart": "삼성전자",
        "name_krx": "삼성전자",
        "market": "KOSPI",
        "market_cap": 1759729861008000,
        "isin": "KR7005930003",
    }
    company_path = tmp_path / "company.json"
    company_path.write_text(json.dumps({
        "corp_code": "00126380",
        "corp_name": "삼성전자(주)",
        "corp_cls": "Y",
        "ceo_nm": "전영현, 노태문",
        "induty_code": "264",
        "est_dt": "19690113",
    }, ensure_ascii=False), encoding="utf-8")

    row = _build_row(target, company_path)
    assert row["corp_code"] == "00126380"
    assert row["corp_name"] == "삼성전자(주)"
    assert row["stock_code"] == "005930"
    assert row["market"] == "KOSPI"
    assert row["listed_at"] == "1969-01-13"

    extra = json.loads(row["extra"])
    assert extra["ceo_nm"] == "전영현, 노태문"
    assert extra["market_cap_krw"] == 1759729861008000


def test_companies_build_row_no_company_json(tmp_path):
    """company.json 이 없어도 target 만으로 build 됨."""
    from fingraph.loaders.companies import _build_row

    target = {"corp_code": "X1", "name_krx": "테스트", "market": "KOSPI"}
    row = _build_row(target, tmp_path / "no.json")
    assert row["corp_code"] == "X1"
    assert row["corp_name"] == "테스트"


def test_companies_dry_run(tmp_path):
    from fingraph.loaders.companies import load_companies

    targets = tmp_path / "targets.jsonl"
    bulk = tmp_path / "bulk"
    targets.write_text(
        json.dumps({"corp_code": "C1", "name_krx": "A", "market": "KOSPI"}) + "\n"
        + json.dumps({"corp_code": "C2", "name_krx": "B", "market": "KOSDAQ"}) + "\n",
        encoding="utf-8",
    )

    stats = load_companies(targets_path=targets, bulk_root=bulk, dry_run=True)
    assert stats.inserted == 2
    assert stats.batches == 1
    assert "INSERT INTO master.companies" in stats.sql_preview[0]


# ── filings ──────────────────────────────────────────────────────────
def test_filings_build_row():
    from fingraph.loaders.filings import _build_row

    row = _build_row("00126380", {
        "rcept_no": "20240315000001",
        "corp_name": "삼성전자",
        "report_nm": "사업보고서 (2023.12)",
        "rcept_dt": "20240315",
        "flr_nm": "삼성전자",
        "pblntf_ty": "A",
    })
    assert row["rcept_no"] == "20240315000001"
    assert row["corp_code"] == "00126380"
    assert row["rcept_dt"] == "2024-03-15"
    assert row["pblntf_ty"] == "A"
    assert json.loads(row["raw"])["report_nm"] == "사업보고서 (2023.12)"


def test_filings_build_row_no_rcept_no():
    from fingraph.loaders.filings import _build_row

    assert _build_row("X", {"report_nm": "no rcept"}) is None


def test_filings_dry_run(tmp_path):
    from fingraph.loaders.filings import load_filings

    bulk = tmp_path / "bulk"
    corp_dir = bulk / "00126380"
    corp_dir.mkdir(parents=True)
    (corp_dir / "filings.jsonl").write_text(
        json.dumps({"rcept_no": "1", "report_nm": "a", "rcept_dt": "20240101", "pblntf_ty": "A"}) + "\n"
        + json.dumps({"rcept_no": "2", "report_nm": "b", "rcept_dt": "20240301", "pblntf_ty": "A"}) + "\n",
        encoding="utf-8",
    )

    stats = load_filings(bulk_root=bulk, dry_run=True)
    assert stats.inserted == 2


# ── financials ──────────────────────────────────────────────────────
def test_financials_build_row():
    from fingraph.loaders.financials import _build_row

    row = _build_row("00126380", 2023, {
        "bsns_year": "2023",
        "reprt_code": "11011",
        "fs_div": "CFS",
        "sj_div": "BS",
        "account_id": "ifrs-full_Assets",
        "account_nm": "자산총계",
        "thstrm_amount": "455,905,980,000,000",
        "frmtrm_amount": "448,424,507,000,000",
        "ord": "1",
    })
    assert row["corp_code"] == "00126380"
    assert row["bsns_year"] == 2023
    assert row["fs_div"] == "CFS"
    assert row["thstrm_amount"] == 455_905_980_000_000
    assert row["frmtrm_amount"] == 448_424_507_000_000
    assert row["ord"] == 1
    assert row["account_id"] == "ifrs-full_Assets"


def test_financials_build_row_no_account_name():
    """account_nm 비어있으면 skip."""
    from fingraph.loaders.financials import _build_row

    assert _build_row("X", 2023, {"account_nm": ""}) is None
    assert _build_row("X", 2023, {"account_nm": None}) is None


def test_financials_dry_run(tmp_path):
    from fingraph.loaders.financials import load_financials

    bulk = tmp_path / "bulk"
    corp_dir = bulk / "00126380" / "financials"
    corp_dir.mkdir(parents=True)
    (corp_dir / "2023_annual_CFS.jsonl").write_text(
        json.dumps({"sj_div": "BS", "account_nm": "자산총계",
                    "thstrm_amount": "1000", "reprt_code": "11011", "fs_div": "CFS"}) + "\n"
        + json.dumps({"sj_div": "BS", "account_nm": "부채총계",
                      "thstrm_amount": "500", "reprt_code": "11011", "fs_div": "CFS"}) + "\n",
        encoding="utf-8",
    )

    stats = load_financials(bulk_root=bulk, dry_run=True, progress=False)
    assert stats.inserted == 2


def test_financials_handles_empty_jsonl(tmp_path):
    """0-byte JSONL (재무 데이터 없는 연도) — 정상 skip."""
    from fingraph.loaders.financials import load_financials

    bulk = tmp_path / "bulk"
    corp_dir = bulk / "X" / "financials"
    corp_dir.mkdir(parents=True)
    (corp_dir / "2023_annual_CFS.jsonl").write_text("", encoding="utf-8")

    stats = load_financials(bulk_root=bulk, dry_run=True, progress=False)
    assert stats.inserted == 0
