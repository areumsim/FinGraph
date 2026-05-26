"""KRX 상장사 + 주요 지수 구성 종목 다운로드.

사용:
    python scripts/ingest/download_listings.py
    python scripts/ingest/download_listings.py --markets KOSPI,KOSDAQ
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fingraph.config import get_settings  # noqa: E402
from fingraph.ingestion.krx_client import KrxClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="KRX 상장사 + 지수 구성 종목")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="저장 디렉토리 (기본: data/raw/krx)")
    parser.add_argument("--markets", type=str, default="ALL",
                        help="마스터 다운로드 시장 (ALL|KOSPI|KOSDAQ|KONEX), 쉼표 구분 다중")
    parser.add_argument("--indices", type=str, default="KOSPI200,KOSDAQ150",
                        help="구성 종목 다운로드 지수 목록 (쉼표 구분)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    out_dir = args.out_dir or (s.ingest_raw_dir / "krx")

    print(f"[INFO] KRX → {out_dir}")
    print(f"[INFO] markets={args.markets}, indices={args.indices}")

    if args.dry_run:
        print("[DRY] 실행 안 함")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)

    with KrxClient(base_url=s.krx_base_url) as client:
        # 상장사 마스터
        for market in [m.strip() for m in args.markets.split(",")]:
            print(f"[INFO] fetching listings: {market}")
            try:
                df = client.fetch_listed_companies(market=market)
                path = out_dir / f"listings_{market.lower()}.csv"
                df.to_csv(path, index=False, encoding="utf-8-sig")
                print(f"[OK] {path} ({len(df):,} rows)")
            except Exception as e:
                print(f"[WARN] {market} 실패: {e}", file=sys.stderr)

        # 지수 구성
        for idx in [i.strip() for i in args.indices.split(",")]:
            print(f"[INFO] fetching index constituents: {idx}")
            try:
                df = client.fetch_index_constituents(index_name=idx)
                path = out_dir / f"index_{idx.lower()}.csv"
                df.to_csv(path, index=False, encoding="utf-8-sig")
                print(f"[OK] {path} ({len(df):,} rows)")
            except Exception as e:
                print(f"[WARN] {idx} 실패: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
