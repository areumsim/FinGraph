"""SQL Agent tools 데모 CLI (LLM 없이 직접 호출).

사용:
    python scripts/query_demo.py revenue 삼성전자 2023
    python scripts/query_demo.py info 005930
    python scripts/query_demo.py compare-top KOSPI 5 revenue 2023
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.tools import (  # noqa: E402
    compare_companies, get_company_info, get_operating_income, get_revenue,
    list_companies_by_market, lookup_company,
)


def _resolve_corp_code(name_or_code: str) -> str | None:
    """이름이면 lookup 후 corp_code, 8자리 숫자면 그대로."""
    if name_or_code.isdigit() and len(name_or_code) == 8:
        return name_or_code
    hits = lookup_company(name_or_code, limit=1)
    return hits[0]["corp_code"] if hits else None


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("lookup")
    sp.add_argument("query")

    sp = sub.add_parser("info")
    sp.add_argument("company", help="이름 또는 corp_code")

    sp = sub.add_parser("revenue")
    sp.add_argument("company")
    sp.add_argument("year", type=int)

    sp = sub.add_parser("opinc")
    sp.add_argument("company")
    sp.add_argument("year", type=int)

    sp = sub.add_parser("compare-top")
    sp.add_argument("market", choices=["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"])
    sp.add_argument("limit", type=int)
    sp.add_argument("metric", choices=["revenue", "operating_income", "net_income"])
    sp.add_argument("year", type=int)

    args = p.parse_args()

    if args.cmd == "lookup":
        for r in lookup_company(args.query):
            print(f"{r['corp_code']}  {r['name']:30s} {r['market']:10s} (score={r['score']})")
        return 0

    if args.cmd == "info":
        cc = _resolve_corp_code(args.company)
        if not cc:
            print(f"회사 미식별: {args.company}", file=sys.stderr)
            return 1
        info = get_company_info(cc)
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.cmd in ("revenue", "opinc"):
        cc = _resolve_corp_code(args.company)
        if not cc:
            print(f"회사 미식별: {args.company}", file=sys.stderr)
            return 1
        info = get_company_info(cc)
        fn = get_revenue if args.cmd == "revenue" else get_operating_income
        r = fn(cc, args.year)
        if r:
            metric_kr = "매출" if args.cmd == "revenue" else "영업이익"
            print(f"{info['name']} {args.year} {metric_kr} ({r['account_nm']}): "
                  f"{r['value']:,} KRW")
        else:
            print(f"{info['name']} {args.year}: 데이터 없음")
        return 0

    if args.cmd == "compare-top":
        top = list_companies_by_market(args.market, limit=args.limit)
        codes = [c["corp_code"] for c in top]
        print(f"=== {args.market} TOP {args.limit} {args.metric} ({args.year}) ===")
        for r in compare_companies(codes, args.year, args.metric):
            val = f"{r['value']:,}" if r['value'] else "(없음)"
            print(f"  {r['name']:25s} {val}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
