"""DART 사업보고서 본문 zip 다운로드.

PG fin.filings 를 source-of-truth 로 사용 (이미 적재된 메타 기반).
사업보고서만 필터 (`report_nm LIKE '%사업보고서%'`).

저장:
    data/raw/dart_bulk/corp/<corp_code>/documents/<rcept_no>.zip

각 zip 안에는 본문 XML (XBRL/HTML 혼합) + 부속 파일. 평균 ~780KB(zipped)
~ ~10MB(unzipped). 압축 해제는 청킹 단계에서.

진행률 + 이어받기 (이미 받은 zip skip) + 실패 ledger + Ctrl+C 안전 종료.

사용:
    python scripts/ingest/download_documents.py
    python scripts/ingest/download_documents.py --years 2024,2025  # 특정 연도
    python scripts/ingest/download_documents.py --report-pattern '%분기보고서%'
    python scripts/ingest/download_documents.py --limit 10         # smoke
    python scripts/ingest/download_documents.py --retry-failed
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fingraph.config import get_settings  # noqa: E402
from fingraph.ingestion.dart_client import DartClient  # noqa: E402


def _doc_dir(root: Path, corp_code: str) -> Path:
    return root / "corp" / corp_code / "documents"


def _zip_path(root: Path, corp_code: str, rcept_no: str) -> Path:
    return _doc_dir(root, corp_code) / f"{rcept_no}.zip"


def _failed_ledger(root: Path) -> Path:
    return root / "_documents_failed.jsonl"


def _append_failed(root: Path, corp_code: str, rcept_no: str, error: str) -> None:
    with _failed_ledger(root).open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "corp_code": corp_code, "rcept_no": rcept_no,
            "error": error, "ts": time.time(),
        }, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="DART 사업보고서 원문 zip 일괄 다운로드")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="기본: data/raw/dart_bulk")
    parser.add_argument("--report-pattern", type=str, default="%사업보고서%",
                        help="report_nm SQL LIKE 패턴 (기본 사업보고서)")
    parser.add_argument("--years", type=str, default=None,
                        help="대상 연도 쉼표 구분 (기본: 최근 INGEST_YEARS_BACK 년)")
    parser.add_argument("--limit", type=int, default=None,
                        help="상위 N 보고서만 (smoke test)")
    parser.add_argument("--force", action="store_true", help="이미 받은 것도 재다운")
    parser.add_argument("--retry-failed", action="store_true",
                        help="이전 실패만 재시도")
    parser.add_argument("--max-calls", type=int, default=8000,
                        help="DART 일 한도 가드 (10,000 중 8,000 기본)")
    parser.add_argument("--dry-run", action="store_true",
                        help="대상 건수만 표시")
    args = parser.parse_args()

    s = get_settings()
    out_dir = args.out_dir or (s.ingest_raw_dir / "dart_bulk")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 대상 연도
    from datetime import date
    if args.years:
        years = [int(y) for y in args.years.split(",")]
    else:
        cur_year = date.today().year
        years = list(range(cur_year - s.ingest_years_back, cur_year + 1))
    year_min, year_max = min(years), max(years)

    # PG 에서 대상 filings 조회
    import psycopg
    targets: list[tuple[str, str, str]] = []   # (corp_code, rcept_no, report_nm)
    with psycopg.connect(s.postgres_dsn) as conn, conn.cursor() as cur:
        sql = """
            SELECT corp_code, rcept_no, report_nm
            FROM fin.filings
            WHERE report_nm LIKE %s
              AND EXTRACT(YEAR FROM rcept_dt) BETWEEN %s AND %s
            ORDER BY rcept_dt DESC, corp_code
        """
        cur.execute(sql, (args.report_pattern, year_min, year_max + 1))   # +1 → 보고서가 익년에 접수되는 경우
        targets = cur.fetchall()

    # retry-failed: ledger 에서 rcept_no 만 추리기
    if args.retry_failed:
        fl = _failed_ledger(out_dir)
        if not fl.exists():
            print("[INFO] 실패 ledger 없음 — 재시도 대상 없음")
            return 0
        failed_rcepts: set[str] = set()
        with fl.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    failed_rcepts.add(json.loads(line)["rcept_no"])
        targets = [t for t in targets if t[1] in failed_rcepts]
        fl.unlink()        # 새 시도 시작 — ledger 초기화
        print(f"[INFO] 실패 재시도: {len(targets):,} 건")

    if args.limit:
        targets = targets[:args.limit]

    print(f"[INFO] pattern  : {args.report_pattern}")
    print(f"[INFO] years    : {year_min}~{year_max}")
    print(f"[INFO] targets  : {len(targets):,} 보고서 ({len(set(t[0] for t in targets)):,} 회사)")
    print(f"[INFO] out_dir  : {out_dir}")

    if args.dry_run:
        print("[DRY] 실행 안 함")
        # 샘플 5개 표시
        for cc, rn, nm in targets[:5]:
            print(f"  - {cc}  {rn}  {nm[:80]}")
        return 0

    if not s.dart_api_key:
        print("[ERROR] DART_API_KEY 미설정", file=sys.stderr)
        return 2

    # 우아한 종료
    stop = {"flag": False}

    def _on_sigint(*_):
        if stop["flag"]:
            print("\n[ABORT] 두 번째 신호 — 즉시 종료", file=sys.stderr)
            sys.exit(130)
        print("\n[INFO] Ctrl+C — 현재 보고서 완료 후 종료", file=sys.stderr)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(it, **_):  # type: ignore[no-redef]
            return it

    summary = Counter()
    call_count = 0
    bytes_total = 0

    with DartClient(api_key=s.dart_api_key, rate_limit_per_sec=s.ingest_rate_limit_per_sec) as client:
        for corp_code, rcept_no, report_nm in tqdm(targets, desc="documents", unit="rpt"):
            if stop["flag"]:
                break
            zip_path = _zip_path(out_dir, corp_code, rcept_no)
            if zip_path.exists() and zip_path.stat().st_size > 0 and not args.force:
                summary["skip"] += 1
                continue
            if call_count >= args.max_calls:
                print(f"\n[STOP] max_calls={args.max_calls} 도달", file=sys.stderr)
                break

            try:
                content = client.download_filing_document(rcept_no)
                if not content or len(content) < 100:
                    raise RuntimeError(f"empty or too small response: {len(content)}B")
                zip_path.parent.mkdir(parents=True, exist_ok=True)
                # 원자적 쓰기: tmp → rename
                tmp = zip_path.with_suffix(".zip.tmp")
                tmp.write_bytes(content)
                tmp.rename(zip_path)
                summary["ok"] += 1
                bytes_total += len(content)
            except Exception as e:
                _append_failed(out_dir, corp_code, rcept_no, str(e))
                summary["fail"] += 1
            call_count += 1

    print()
    print("=" * 60)
    print(f"[DONE] API 호출 수 : {call_count:,}")
    print(f"[DONE] ok          : {summary['ok']:,}")
    print(f"[DONE] skip(이미)  : {summary['skip']:,}")
    print(f"[DONE] fail        : {summary['fail']:,}")
    print(f"[DONE] 다운로드량   : {bytes_total / (1024 * 1024):.1f} MB")
    if summary["fail"] > 0:
        print(f"[DONE] ledger      : {_failed_ledger(out_dir)} (재시도: --retry-failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
