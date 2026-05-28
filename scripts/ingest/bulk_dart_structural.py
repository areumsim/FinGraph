"""DART 정형 지배구조 API 일괄 수집 — 자회사 / 임원 / 최대주주.

P2 그래프 노드/관계의 source-of-truth. PG fin.filings (이미 적재) 의 사업보고서만 대상.

저장:
    data/raw/dart_bulk/corp/<corp_code>/subsidiaries/{year}.jsonl
    data/raw/dart_bulk/corp/<corp_code>/executives/{year}.jsonl
    data/raw/dart_bulk/corp/<corp_code>/shareholders/{year}.jsonl

각 파일은 회사 × 연도 × API 응답 1건.
이어받기 (파일 존재 skip), 실패 ledger, max_calls 가드.

사용:
    python scripts/ingest/bulk_dart_structural.py
    python scripts/ingest/bulk_dart_structural.py --apis subsidiaries  # 1종만
    python scripts/ingest/bulk_dart_structural.py --years 2023,2024
    python scripts/ingest/bulk_dart_structural.py --limit 10
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

from autonexusgraph.config import get_settings  # noqa: E402
from autonexusgraph.ingestion.dart_client import DartClient  # noqa: E402


# API 종류별 (디렉토리명, 메서드명)
APIS: dict[str, str] = {
    "subsidiaries":  "get_other_corp_investment",
    "executives":    "get_executive_status",
    "shareholders":  "get_largest_shareholder",
    "employees":     "get_employee_status",
}


def _out_path(out_dir: Path, corp_code: str, kind: str, year: int) -> Path:
    return out_dir / "corp" / corp_code / kind / f"{year}.jsonl"


def _failed_ledger(out_dir: Path) -> Path:
    return out_dir / "_structural_failed.jsonl"


def _append_failed(out_dir: Path, corp_code: str, year: int, kind: str, error: str) -> None:
    with _failed_ledger(out_dir).open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "corp_code": corp_code, "year": year, "kind": kind,
            "error": error, "ts": time.time(),
        }, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="DART 지배구조 정형 API 일괄")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--apis", type=str, default=",".join(APIS),
                        help=f"쉼표 구분. 가능: {list(APIS)}")
    parser.add_argument("--years", type=str, default=None,
                        help="쉼표 구분 (기본: 최근 INGEST_YEARS_BACK 년)")
    parser.add_argument("--limit", type=int, default=None,
                        help="처음 N 회사만")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-calls", type=int, default=8000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    out_dir = args.out_dir or (s.ingest_raw_dir / "dart_bulk")

    apis = [a.strip() for a in args.apis.split(",") if a.strip() in APIS]
    if not apis:
        print(f"[ERROR] valid apis 0개. {list(APIS)}", file=sys.stderr)
        return 2

    if args.years:
        years = [int(y) for y in args.years.split(",")]
    else:
        from datetime import date
        cy = date.today().year
        years = list(range(cy - s.ingest_years_back, cy))

    # 대상 회사 — PG master.companies (이미 적재됨)
    import psycopg
    with psycopg.connect(s.postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT corp_code, corp_name FROM master.companies "
                    "WHERE is_active = TRUE ORDER BY corp_code")
        companies = cur.fetchall()
    if args.limit:
        companies = companies[:args.limit]

    # retry-failed
    if args.retry_failed:
        fl = _failed_ledger(out_dir)
        if not fl.exists():
            print("[INFO] 실패 ledger 없음")
            return 0
        failed = set()
        with fl.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    failed.add((d["corp_code"], d["year"], d["kind"]))
        fl.unlink()
        print(f"[INFO] 실패 재시도: {len(failed)}건")

    total_targets = len(companies) * len(years) * len(apis)
    print(f"[INFO] companies : {len(companies):,}")
    print(f"[INFO] years     : {years}")
    print(f"[INFO] apis      : {apis}")
    print(f"[INFO] targets   : {total_targets:,} (회사 × 연도 × API)")

    if args.dry_run:
        return 0

    if not s.dart_api_key:
        print("[ERROR] DART_API_KEY 미설정", file=sys.stderr)
        return 2

    stop = {"flag": False}

    def _on_sigint(*_):
        if stop["flag"]:
            sys.exit(130)
        print("\n[INFO] Ctrl+C — 현재 항목 완료 후 종료", file=sys.stderr)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(it, **_):  # type: ignore[no-redef]
            return it

    summary = Counter()
    call_count = 0

    with DartClient(api_key=s.dart_api_key, rate_limit_per_sec=s.ingest_rate_limit_per_sec) as client:
        pbar = tqdm(total=total_targets, desc="structural", unit="call")
        for corp_code, _name in companies:
            if stop["flag"]:
                break
            for year in years:
                for api_kind in apis:
                    if stop["flag"]:
                        break
                    if call_count >= args.max_calls:
                        print(f"\n[STOP] max_calls={args.max_calls} 도달", file=sys.stderr)
                        stop["flag"] = True
                        break

                    if args.retry_failed:
                        if (corp_code, year, api_kind) not in failed:
                            summary["skip-not-failed"] += 1
                            pbar.update(1)
                            continue

                    out_path = _out_path(out_dir, corp_code, api_kind, year)
                    if out_path.exists() and not args.force:
                        summary["skip"] += 1
                        pbar.update(1)
                        continue

                    method = getattr(client, APIS[api_kind])
                    try:
                        rows = method(corp_code, str(year))
                    except Exception as e:
                        _append_failed(out_dir, corp_code, year, api_kind, str(e))
                        summary["fail"] += 1
                        call_count += 1
                        pbar.update(1)
                        continue

                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with out_path.open("w", encoding="utf-8") as f:
                        for r in rows:
                            f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    summary["ok" if rows else "empty"] += 1
                    call_count += 1
                    pbar.update(1)
        pbar.close()

    print()
    print("=" * 60)
    print(f"[DONE] API 호출 수 : {call_count:,}")
    for k in ("ok", "empty", "skip", "fail"):
        print(f"[DONE] {k:7s}     : {summary[k]:,}")
    if summary["fail"]:
        print(f"[DONE] failed_ledger: {_failed_ledger(out_dir)} (재시도: --retry-failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
