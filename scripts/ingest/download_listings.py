"""KRX 상장사 + 시가총액 상위 N 종목 다운로드.

PRD §3.3: KOSPI 상위 200 + KOSDAQ 상위 100 (시가총액 기준 ≈ 공식 지수 대용).

사용:
    # 전체 마스터 (KOSPI + KOSDAQ)
    python scripts/ingest/download_listings.py

    # 시가총액 상위 N만
    python scripts/ingest/download_listings.py --top-kospi 200 --top-kosdaq 100
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.config import get_settings  # noqa: E402
from autonexusgraph.ingestion.krx_client import KrxClient  # noqa: E402


def _write_listings_csv(listings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["stock_code", "name", "market", "market_cap", "sector", "isin"])
        for l in listings:
            writer.writerow([l.stock_code, l.name, l.market, l.market_cap or "",
                             l.sector or "", l.isin or ""])


def main() -> int:
    parser = argparse.ArgumentParser(description="KRX 상장사 + 시가총액 상위 N")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="저장 디렉토리 (기본: data/raw/krx)")
    parser.add_argument("--top-kospi", type=int, default=200,
                        help="KOSPI 시가총액 상위 N (기본 200)")
    parser.add_argument("--top-kosdaq", type=int, default=100,
                        help="KOSDAQ 시가총액 상위 N (기본 100)")
    parser.add_argument("--all-listings", action="store_true",
                        help="시가총액 상위 외에도 전체 마스터 CSV 저장")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    out_dir = args.out_dir or (s.ingest_raw_dir / "krx")

    print(f"[INFO] KRX → {out_dir}")
    print(f"[INFO] top: KOSPI {args.top_kospi}, KOSDAQ {args.top_kosdaq}")

    if args.dry_run:
        print("[DRY] 실행 안 함")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        client = KrxClient()
    except ImportError as e:
        print(f"[ERROR] FinanceDataReader 미설치. pip install 'autonexusgraph[ingest]'\n  {e}",
              file=sys.stderr)
        return 2

    # 시가총액 상위 N (이게 핵심)
    for market, n, label in [("KOSPI", args.top_kospi, "kospi"),
                              ("KOSDAQ", args.top_kosdaq, "kosdaq")]:
        try:
            top = client.top_n_by_market_cap(market, n=n)
            path = out_dir / f"top_{label}_{n}.csv"
            _write_listings_csv(top, path)
            print(f"[OK] {path} ({len(top):,} rows; top cap: {top[0].name} = {top[0].market_cap:,})")
        except Exception as e:
            print(f"[WARN] {market} top {n} 실패: {e}", file=sys.stderr)

    # (선택) 전체 마스터도
    if args.all_listings:
        for market in ["KOSPI", "KOSDAQ"]:
            try:
                df = client.fetch_listed_companies(market)
                path = out_dir / f"all_{market.lower()}.csv"
                df.to_csv(path, index=False, encoding="utf-8-sig")
                print(f"[OK] {path} ({len(df):,} rows)")
            except Exception as e:
                print(f"[WARN] {market} 전체 실패: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
