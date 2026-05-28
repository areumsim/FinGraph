"""DART 사업/반기/분기 보고서 메타 + 원문 다운로드.

사용:
    # 단일 회사
    python scripts/ingest/download_business_reports.py --corp-code 00126380 \
        --start 20220101 --end 20241231

    # corp_codes CSV 기반 일괄
    python scripts/ingest/download_business_reports.py --corp-codes-csv \
        data/raw/dart/corp_codes_listed.csv --limit 5

PRD §3.3: 1차 범위 = 코스피200+코스닥100 × 최근 3년.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.config import get_settings  # noqa: E402
from autonexusgraph.ingestion.dart_client import DartClient, Filing  # noqa: E402


def _save_filings_meta(filings: list[Filing], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for fl in filings:
            f.write(json.dumps(fl.__dict__, ensure_ascii=False) + "\n")


def _download_for_corp(
    client: DartClient,
    corp_code: str,
    bgn_de: str,
    end_de: str,
    out_root: Path,
    download_documents: bool = False,
) -> int:
    filings = list(client.iter_filings(corp_code=corp_code, bgn_de=bgn_de, end_de=end_de, pblntf_ty="A"))
    meta_path = out_root / corp_code / "filings.jsonl"
    _save_filings_meta(filings, meta_path)
    print(f"[OK] {corp_code}: {len(filings):,} filings → {meta_path}")

    if download_documents:
        docs_dir = out_root / corp_code / "documents"
        docs_dir.mkdir(parents=True, exist_ok=True)
        for fl in filings:
            doc_path = docs_dir / f"{fl.rcept_no}.zip"
            if doc_path.exists():
                continue
            try:
                content = client.download_filing_document(fl.rcept_no)
                doc_path.write_bytes(content)
            except Exception as e:
                print(f"[WARN] {fl.rcept_no} 다운로드 실패: {e}", file=sys.stderr)
    return len(filings)


def main() -> int:
    parser = argparse.ArgumentParser(description="DART 보고서 메타·원문 다운로드")
    parser.add_argument("--corp-code", type=str, help="단일 회사 8자리 corp_code")
    parser.add_argument("--corp-codes-csv", type=Path,
                        help="corp_codes_listed.csv 경로 (일괄 다운로드)")
    parser.add_argument("--start", type=str, default=None, help="YYYYMMDD (기본: 3년 전)")
    parser.add_argument("--end", type=str, default=None, help="YYYYMMDD (기본: 오늘)")
    parser.add_argument("--limit", type=int, default=None, help="CSV 기반 상위 N개 제한")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="저장 루트 (기본: data/raw/dart/reports)")
    parser.add_argument("--download-documents", action="store_true",
                        help="rcept_no 별 원문 zip 도 다운로드 (느림)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.corp_code and not args.corp_codes_csv:
        parser.error("--corp-code 또는 --corp-codes-csv 중 하나 필수")

    s = get_settings()
    out_root = args.out_dir or (s.ingest_raw_dir / "dart" / "reports")

    if not args.end:
        from datetime import date
        args.end = date.today().strftime("%Y%m%d")
    if not args.start:
        from datetime import date
        args.start = f"{date.today().year - s.ingest_years_back}0101"

    print(f"[INFO] period: {args.start} ~ {args.end}, out={out_root}")

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

    total = 0
    with DartClient(api_key=s.dart_api_key, rate_limit_per_sec=s.ingest_rate_limit_per_sec) as client:
        for cc in targets:
            try:
                total += _download_for_corp(client, cc, args.start, args.end, out_root,
                                            download_documents=args.download_documents)
            except Exception as e:
                print(f"[WARN] {cc} 실패: {e}", file=sys.stderr)
    print(f"[DONE] total filings: {total:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
