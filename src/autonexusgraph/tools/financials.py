"""SQL Agent 도구 — 사전 정의 함수 풀 (PRD §7.5.10).

자유 SQL 금지. LLM 은 함수명 + 파라미터만 결정.
READ-ONLY 쿼리만. PG 측에서도 read-only role 권장 (운영 시).

각 함수는 dict / list[dict] 반환 → JSON serializable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from ..db.postgres import get_connection


# ── 회사 식별 ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CompanyRef:
    corp_code: str
    name: str
    stock_code: str | None
    market: str | None


def lookup_company(query: str, limit: int = 5) -> list[dict]:
    """이름·종목코드·corp_code 로 회사 식별.

    매칭 우선순위: corp_code 정확 → stock_code 정확 → name 정확 → name 부분일치
    """
    q = (query or "").strip()
    if not q:
        return []
    conn = get_connection()
    with conn.cursor() as cur:
        # 정확 매치들 모두
        cur.execute("""
            SELECT corp_code, corp_name, stock_code, market,
                   CASE WHEN corp_code = %(q)s THEN 100
                        WHEN stock_code = %(q)s THEN 90
                        WHEN corp_name = %(q)s THEN 80
                        WHEN corp_name ILIKE %(q)s || '%%' THEN 60
                        WHEN corp_name ILIKE '%%' || %(q)s || '%%' THEN 40
                        ELSE 0 END AS score
            FROM master.companies
            WHERE corp_code = %(q)s
               OR stock_code = %(q)s
               OR corp_name ILIKE '%%' || %(q)s || '%%'
            ORDER BY score DESC, corp_name ASC
            LIMIT %(lim)s
        """, {"q": q, "lim": limit})
        rows = cur.fetchall()
    conn.commit()
    return [{
        "corp_code": r[0], "name": r[1], "stock_code": r[2],
        "market": r[3], "score": r[4],
    } for r in rows]


def get_company_info(corp_code: str) -> dict | None:
    """단일 회사 상세."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT corp_code, corp_name, stock_code, market, sector, industry,
                   listed_at, is_active, extra
            FROM master.companies WHERE corp_code = %s
        """, (corp_code,))
        r = cur.fetchone()
    conn.commit()
    if not r:
        return None
    extra = r[8] if isinstance(r[8], dict) else (json.loads(r[8]) if r[8] else {})
    return {
        "corp_code": r[0], "name": r[1], "stock_code": r[2],
        "market": r[3], "sector": r[4], "industry": r[5],
        "listed_at": r[6].isoformat() if r[6] else None,
        "is_active": r[7],
        "ceo": extra.get("ceo_nm"),
        "market_cap_krw": extra.get("market_cap_krw"),
        "homepage": extra.get("hm_url"),
        "established": extra.get("est_dt"),
        "fiscal_year_end_month": extra.get("acc_mt"),
    }


# ── 재무 정확값 조회 ─────────────────────────────────────────────────

# 표준 계정명 매핑 — 회사마다 표기 다른 것 정규화
_REVENUE_ACCOUNTS = ("매출액", "수익(매출액)", "영업수익", "영업매출", "수익")
_OP_INCOME_ACCOUNTS = ("영업이익", "영업이익(손실)", "영업손익")
_NET_INCOME_ACCOUNTS = ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익")
_ASSETS_ACCOUNTS = ("자산총계",)
_LIABILITIES_ACCOUNTS = ("부채총계",)
_EQUITY_ACCOUNTS = ("자본총계",)


def _query_amount(
    corp_code: str, year: int, account_candidates: tuple[str, ...],
    sj_div: str = "IS", fs_div: str = "CFS",
) -> dict | None:
    """주어진 계정명 후보 중 첫 매칭의 금액 반환."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT account_nm, thstrm_amount, frmtrm_amount, fs_div, sj_div, reprt_code
            FROM fin.financials
            WHERE corp_code = %s AND bsns_year = %s AND fs_div = %s AND sj_div = %s
              AND account_nm = ANY(%s)
              AND thstrm_amount IS NOT NULL
            ORDER BY ord NULLS LAST LIMIT 1
        """, (corp_code, year, fs_div, sj_div, list(account_candidates)))
        r = cur.fetchone()
    conn.commit()
    if not r:
        return None
    return {
        "account_nm": r[0],
        "value": int(r[1]) if isinstance(r[1], (int, Decimal)) else r[1],
        "prev_value": int(r[2]) if r[2] is not None else None,
        "fs_div": r[3], "sj_div": r[4], "reprt_code": r[5],
        "currency": "KRW",
    }


def get_revenue(corp_code: str, year: int, fs_div: str = "CFS") -> dict | None:
    """매출액 (IS, 다중 계정명 호환)."""
    return _query_amount(corp_code, year, _REVENUE_ACCOUNTS, "IS", fs_div)


def get_operating_income(corp_code: str, year: int, fs_div: str = "CFS") -> dict | None:
    """영업이익 (IS)."""
    return _query_amount(corp_code, year, _OP_INCOME_ACCOUNTS, "IS", fs_div)


def get_net_income(corp_code: str, year: int, fs_div: str = "CFS") -> dict | None:
    """당기순이익 (IS)."""
    return _query_amount(corp_code, year, _NET_INCOME_ACCOUNTS, "IS", fs_div)


def get_balance_sheet_item(
    corp_code: str, year: int, item: str, fs_div: str = "CFS",
) -> dict | None:
    """BS 항목 (자산총계/부채총계/자본총계/등)."""
    item_map = {
        "자산총계": _ASSETS_ACCOUNTS, "총자산": _ASSETS_ACCOUNTS,
        "부채총계": _LIABILITIES_ACCOUNTS, "총부채": _LIABILITIES_ACCOUNTS,
        "자본총계": _EQUITY_ACCOUNTS, "총자본": _EQUITY_ACCOUNTS,
    }
    candidates = item_map.get(item, (item,))
    return _query_amount(corp_code, year, candidates, "BS", fs_div)


# ── 비교·집계 ────────────────────────────────────────────────────────

_METRIC_ACCOUNTS: dict[str, tuple[str, ...]] = {
    "revenue":          _REVENUE_ACCOUNTS,
    "operating_income": _OP_INCOME_ACCOUNTS,
    "net_income":       _NET_INCOME_ACCOUNTS,
}


def compare_companies(
    corp_codes: list[str], year: int, metric: str = "revenue", fs_div: str = "CFS",
) -> list[dict]:
    """여러 회사 단일 지표 비교 (정렬 내림차순).

    단일 PG round-trip — corp_codes ARRAY 로 names + 금액을 한 번에 조회.
    """
    accounts = _METRIC_ACCOUNTS.get(metric)
    if accounts is None:
        raise ValueError(f"unknown metric: {metric}. 가능: revenue/operating_income/net_income")
    if not corp_codes:
        return []

    conn = get_connection()
    with conn.cursor() as cur:
        # 1) 회사명 일괄 조회.
        cur.execute("""
            SELECT corp_code, corp_name FROM master.companies
             WHERE corp_code = ANY(%s)
        """, (list(corp_codes),))
        name_by_cc = {r[0]: r[1] for r in cur.fetchall()}

        # 2) 각 corp_code 의 첫 매칭 계정 (ord 우선) 한 번에 — LATERAL.
        cur.execute("""
            SELECT cc, account_nm, value FROM (
                SELECT cc,
                       (SELECT account_nm FROM fin.financials f
                         WHERE f.corp_code = cc AND f.bsns_year = %s
                           AND f.fs_div = %s AND f.sj_div = 'IS'
                           AND f.account_nm = ANY(%s)
                           AND f.thstrm_amount IS NOT NULL
                         ORDER BY ord NULLS LAST LIMIT 1) AS account_nm,
                       (SELECT thstrm_amount FROM fin.financials f
                         WHERE f.corp_code = cc AND f.bsns_year = %s
                           AND f.fs_div = %s AND f.sj_div = 'IS'
                           AND f.account_nm = ANY(%s)
                           AND f.thstrm_amount IS NOT NULL
                         ORDER BY ord NULLS LAST LIMIT 1) AS value
                  FROM UNNEST(%s::text[]) AS cc
            ) t
        """, (year, fs_div, list(accounts),
              year, fs_div, list(accounts),
              list(corp_codes)))
        fact_by_cc = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    conn.commit()

    results: list[dict] = []
    for cc in corp_codes:
        account_nm, val = fact_by_cc.get(cc, (None, None))
        results.append({
            "corp_code":  cc,
            "name":       name_by_cc.get(cc),
            "value":      int(val) if isinstance(val, (int, Decimal)) else val,
            "account_nm": account_nm,
            "year":       year,
            "metric":     metric,
        })
    results.sort(key=lambda r: r["value"] if r["value"] is not None else -1, reverse=True)
    return results


def list_companies_by_market(market: str, limit: int = 50) -> list[dict]:
    """시장(KOSPI/KOSDAQ) 별 회사 — 시가총액 내림차순."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT corp_code, corp_name, stock_code,
                   (extra->>'market_cap_krw')::numeric AS cap
            FROM master.companies
            WHERE market = %s AND is_active = TRUE
            ORDER BY cap DESC NULLS LAST
            LIMIT %s
        """, (market, limit))
        rows = cur.fetchall()
    conn.commit()
    return [
        {"corp_code": r[0], "name": r[1], "stock_code": r[2],
         "market_cap_krw": int(r[3]) if r[3] else None}
        for r in rows
    ]
