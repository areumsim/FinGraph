"""DART corp_code 마스터 다운로드.

사용:
    python scripts/ingest/download_corp_codes.py
    python scripts/ingest/download_corp_codes.py --out-dir data/raw/dart --limit 10
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# src/ 를 sys.path 에 추가 (editable install 안한 환경 호환)
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fingraph.config import get_settings  # noqa: E402
from fingraph.ingestion.dart_client import DartClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="DART 회사 코드 마스터 다운로드")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="저장 디렉토리 (기본: data/raw/dart)")
    parser.add_argument("--limit", type=int, default=None,
                        help="상장사 추출 시 상위 N개로 제한 (디버깅)")
    parser.add_argument("--listed-only", action="store_true", default=True,
                        help="상장사(stock_code 있음)만 CSV 출력 (기본 True)")
    parser.add_argument("--dry-run", action="store_true",
                        help="다운로드 없이 인자만 확인")
    args = parser.parse_args()

    s = get_settings()
    out_dir = args.out_dir or (s.ingest_raw_dir / "dart")

    print(f"[INFO] DART corp_codes → {out_dir}")
    print(f"[INFO] API key: {'설정됨' if s.dart_api_key else '미설정'}")

    if args.dry_run:
        print("[DRY] 실행 안 함")
        return 0

    if not s.dart_api_key:
        print("[ERROR] DART_API_KEY 가 .env 에 없습니다. opendart.fss.or.kr 가입 후 키 발급 필요.",
              file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)

    with DartClient(api_key=s.dart_api_key, rate_limit_per_sec=s.ingest_rate_limit_per_sec) as client:
        zip_path = out_dir / "corpCode.xml.zip"
        print(f"[INFO] downloading zip...")
        zip_bytes = client.fetch_corp_codes_zip()
        zip_path.write_bytes(zip_bytes)
        print(f"[OK] saved {zip_path} ({len(zip_bytes):,} bytes)")

        # CSV 로도 평탄화 저장 — 상장사만 (혹은 전체)
        csv_path = out_dir / ("corp_codes_listed.csv" if args.listed_only else "corp_codes_all.csv")
        rows = client.parse_corp_codes(zip_bytes)
        count = 0
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["corp_code", "corp_name", "stock_code", "modify_date"])
            for row in rows:
                if args.listed_only and not row.stock_code:
                    continue
                writer.writerow([row.corp_code, row.corp_name, row.stock_code or "", row.modify_date])
                count += 1
                if args.limit and count >= args.limit:
                    break
        print(f"[OK] saved {csv_path} ({count:,} rows)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
