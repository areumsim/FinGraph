"""DART 재무제표 (XBRL 기반 fnlttSinglAcntAll) 다운로드.

사용:
    # 단일 회사 × 단일 연도
    python scripts/ingest/download_financials.py --corp-code 00126380 --year 2023

    # CSV 일괄 (corp_codes × years_back)
    python scripts/ingest/download_financials.py --corp-codes-csv \
        data/raw/dart/corp_codes_listed.csv --limit 10
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.config import get_settings  # noqa: E402
from autonexusgraph.ingestion.dart_client import DartClient  # noqa: E402


REPRT_CODES = {
    "annual": "11011",       # 사업보고서 (Q4 누적)
    "Q3": "11014",
    "half": "11012",
    "Q1": "11013",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="DART 재무제표 다운로드")
    parser.add_argument("--corp-code", type=str, help="단일 회사")
    parser.add_argument("--corp-codes-csv", type=Path, help="회사 일괄")
    parser.add_argument("--year", type=int, action="append",
                        help="연도 (반복 가능). 미지정 시 최근 INGEST_YEARS_BACK 년")
    parser.add_argument("--reports", type=str, default="annual",
                        help="annual|Q3|half|Q1 쉼표 구분")
    parser.add_argument("--fs-div", type=str, default="CFS",
                        choices=["CFS", "OFS"], help="CFS=연결, OFS=별도")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="기본: data/raw/dart/financials")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.corp_code and not args.corp_codes_csv:
        parser.error("--corp-code 또는 --corp-codes-csv 필수")

    s = get_settings()
    out_root = args.out_dir or (s.ingest_raw_dir / "dart" / "financials")

    # 연도 결정
    if args.year:
        years = sorted({int(y) for y in args.year})
    else:
        this_year = date.today().year
        years = list(range(this_year - s.ingest_years_back, this_year))

    report_codes = [REPRT_CODES[r.strip()] for r in args.reports.split(",")]
    print(f"[INFO] years={years} reports={report_codes} fs_div={args.fs_div} → {out_root}")

    if args.dry_run:
        print("[DRY] 실행 안 함")
        return 0

    if not s.dart_api_key:
        print("[ERROR] DART_API_KEY 미설정", file=sys.stderr)
        return 2

    targets: list[str] = []
    if args.corp_code:
        targets.append(args.corp_code)
    if args.corp_codes_csv:
        with args.corp_codes_csv.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if args.limit and i >= args.limit:
                    break
                targets.append(row["corp_code"])

    total_rows = 0
    with DartClient(api_key=s.dart_api_key, rate_limit_per_sec=s.ingest_rate_limit_per_sec) as client:
        for cc in targets:
            corp_dir = out_root / cc
            corp_dir.mkdir(parents=True, exist_ok=True)
            for y in years:
                for rc in report_codes:
                    try:
                        rows = client.get_single_finstat_all(
                            corp_code=cc, bsns_year=str(y),
                            reprt_code=rc, fs_div=args.fs_div,
                        )
                    except Exception as e:
                        print(f"[WARN] {cc}/{y}/{rc} 실패: {e}", file=sys.stderr)
                        continue
                    if not rows:
                        continue
                    out_path = corp_dir / f"{y}_{rc}_{args.fs_div}.jsonl"
                    with out_path.open("w", encoding="utf-8") as f:
                        for r in rows:
                            f.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")
                    total_rows += len(rows)
                    print(f"[OK] {cc}/{y}/{rc}: {len(rows)} rows → {out_path}")
    print(f"[DONE] total rows: {total_rows:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
