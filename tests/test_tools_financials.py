"""SQL Agent 도구 unit 테스트 — PG 연결 mock (integration X)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_conn():
    """get_connection 을 mock 해서 cursor 동작 시뮬레이션."""
    with patch("autonexusgraph.tools.financials.get_connection") as gc:
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        gc.return_value = conn
        yield cur


def test_lookup_company_returns_score(mock_conn):
    from autonexusgraph.tools.financials import lookup_company

    mock_conn.fetchall.return_value = [
        ("00126380", "삼성전자(주)", "005930", "KOSPI", 60),
    ]
    rows = lookup_company("삼성전자")
    assert len(rows) == 1
    assert rows[0]["corp_code"] == "00126380"
    assert rows[0]["score"] == 60


def test_lookup_empty_query():
    from autonexusgraph.tools.financials import lookup_company

    assert lookup_company("") == []
    assert lookup_company(None) == []  # type: ignore[arg-type]


def test_get_revenue_handles_none(mock_conn):
    from autonexusgraph.tools.financials import get_revenue

    mock_conn.fetchone.return_value = None
    assert get_revenue("XX", 2023) is None


def test_get_revenue_returns_dict(mock_conn):
    from autonexusgraph.tools.financials import get_revenue

    mock_conn.fetchone.return_value = (
        "영업수익", 258_935_494_000_000, 250_000_000_000_000, "CFS", "IS", "11011",
    )
    r = get_revenue("00126380", 2023)
    assert r["account_nm"] == "영업수익"
    assert r["value"] == 258_935_494_000_000
    assert r["currency"] == "KRW"


def test_get_balance_sheet_item_normalizes_alias(mock_conn):
    """item='총자산' → 자산총계 후보로 매핑."""
    from autonexusgraph.tools.financials import get_balance_sheet_item

    mock_conn.fetchone.return_value = (
        "자산총계", 455_905_980_000_000, None, "CFS", "BS", "11011",
    )
    r = get_balance_sheet_item("00126380", 2023, "총자산")
    assert r["account_nm"] == "자산총계"


def test_compare_companies_invalid_metric():
    from autonexusgraph.tools.financials import compare_companies

    with pytest.raises(ValueError, match="unknown metric"):
        compare_companies(["X"], 2023, "invalid_metric")


def test_compare_companies_sorts_desc(mock_conn):
    """다중 회사 시 None 은 뒤로, 값은 내림차순."""
    from autonexusgraph.tools.financials import compare_companies

    # get_company_info 와 get_revenue 둘 다 호출됨 — 호출 횟수에 따라 다른 응답
    call_log = []
    def side_effect():
        call_log.append(1)
        # 3 companies × (company_info → revenue) = 6 calls
        # 각각 fetchone 한 번씩
        return [
            ("A", "Aco", "001", "KOSPI", "100", "ind", None, True, {}),  # info 1
            ("매출액", 200, None, "CFS", "IS", "11011"),                       # rev 1
            ("B", "Bco", "002", "KOSPI", "100", "ind", None, True, {}),  # info 2
            ("매출액", 500, None, "CFS", "IS", "11011"),                       # rev 2
            ("C", "Cco", "003", "KOSPI", "100", "ind", None, True, {}),  # info 3
            None,                                                          # rev 3 (없음)
        ][len(call_log) - 1]

    mock_conn.fetchone.side_effect = side_effect
    rows = compare_companies(["A", "B", "C"], 2023, "revenue")
    assert [r["name"] for r in rows] == ["Bco", "Aco", "Cco"]
    assert rows[0]["value"] == 500
    assert rows[-1]["value"] is None
