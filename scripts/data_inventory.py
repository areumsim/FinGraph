"""data/raw 인벤토리 + 정합성 검증.

사용:
    python scripts/data_inventory.py
    python scripts/data_inventory.py --json    # 기계 가독 출력

검증 항목:
- DART
  - corp_codes 마스터 존재 여부 + 상장사 건수
  - dart_bulk: 회사별 company.json/filings.jsonl/financials 존재
  - 누락 회사 (대상 vs 실제)
  - 누락 연도 (대상 vs 실제)
  - 빈 financials (정상이지만 보고)
- KRX
  - top_kospi_200, top_kosdaq_100
- ECOS
  - 시계열별 row 수
- Targets
  - ingest_targets.jsonl + 매칭률
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.config import get_settings  # noqa: E402


def _count_lines(p: Path) -> int:
    if not p.exists() or p.stat().st_size == 0:
        return 0
    with p.open("rb") as f:
        return sum(1 for _ in f)


def _size_human(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


def _section(title: str) -> None:
    print()
    print(f"━━━ {title} " + "━" * (60 - len(title)))


def main() -> int:
    parser = argparse.ArgumentParser(description="data/raw 인벤토리 + 검증")
    parser.add_argument("--root", type=Path, default=None,
                        help="data/raw 디렉토리 (기본: settings)")
    parser.add_argument("--json", action="store_true",
                        help="JSON 출력 (기계 가독)")
    args = parser.parse_args()

    s = get_settings()
    root = args.root or s.ingest_raw_dir
    report: dict = {"root": str(root)}

    # ─── 1. DART 마스터 ─────────────────────────────────────────────
    dart_zip = root / "dart" / "corpCode.xml.zip"
    dart_csv = root / "dart" / "corp_codes_listed.csv"
    listed_count = _count_lines(dart_csv) - 1 if dart_csv.exists() else 0  # 헤더 제외
    report["dart_master"] = {
        "zip_exists": dart_zip.exists(),
        "zip_size": dart_zip.stat().st_size if dart_zip.exists() else 0,
        "listed_csv_exists": dart_csv.exists(),
        "listed_count": max(0, listed_count),
    }

    # ─── 2. KRX 마스터 ─────────────────────────────────────────────
    krx_kospi = root / "krx" / "top_kospi_200.csv"
    krx_kosdaq = root / "krx" / "top_kosdaq_100.csv"
    report["krx"] = {
        "top_kospi_200": max(0, _count_lines(krx_kospi) - 1) if krx_kospi.exists() else 0,
        "top_kosdaq_100": max(0, _count_lines(krx_kosdaq) - 1) if krx_kosdaq.exists() else 0,
    }

    # ─── 3. Targets ────────────────────────────────────────────────
    targets_path = root / "ingest_targets.jsonl"
    targets: list[dict] = []
    if targets_path.exists():
        with targets_path.open(encoding="utf-8") as f:
            targets = [json.loads(line) for line in f if line.strip()]
    report["targets"] = {"path": str(targets_path), "count": len(targets)}

    target_codes = {t["corp_code"] for t in targets}
    target_market: dict[str, list[dict]] = defaultdict(list)
    for t in targets:
        target_market[t["market"]].append(t)

    # ─── 4. DART Bulk (회사별) ────────────────────────────────────
    bulk_root = root / "dart_bulk" / "corp"
    today_year = date.today().year
    expected_years = list(range(today_year - s.ingest_years_back, today_year))

    per_corp: dict[str, dict] = {}
    if bulk_root.exists():
        for corp_dir in sorted(bulk_root.iterdir()):
            if not corp_dir.is_dir():
                continue
            cc = corp_dir.name
            comp = (corp_dir / "company.json").exists()
            filings_path = corp_dir / "filings.jsonl"
            filings_rows = _count_lines(filings_path) if filings_path.exists() else 0
            fin_dir = corp_dir / "financials"
            years_have: dict[int, int] = {}
            if fin_dir.exists():
                for fp in fin_dir.glob("*_annual_CFS.jsonl"):
                    try:
                        y = int(fp.stem.split("_")[0])
                    except ValueError:
                        continue
                    years_have[y] = _count_lines(fp)
            per_corp[cc] = {
                "company": comp,
                "filings_rows": filings_rows,
                "years_present": sorted(years_have.keys()),
                "years_missing": sorted(set(expected_years) - set(years_have.keys())),
                "years_empty": sorted(y for y, n in years_have.items() if n == 0),
                "total_finstat_rows": sum(years_have.values()),
            }

    # 매칭: 대상 ↔ 실제 수집
    not_started = sorted(target_codes - set(per_corp.keys()))
    extras = sorted(set(per_corp.keys()) - target_codes)
    complete = [cc for cc, d in per_corp.items()
                if d["company"] and d["filings_rows"] >= 0 and not d["years_missing"]]
    partial = [cc for cc, d in per_corp.items() if cc not in complete]

    report["dart_bulk"] = {
        "corps_scanned": len(per_corp),
        "target_total": len(targets),
        "complete": len(complete),
        "partial": len(partial),
        "not_started": len(not_started),
        "extras_not_in_targets": len(extras),
        "expected_years": expected_years,
        "total_finstat_rows": sum(d["total_finstat_rows"] for d in per_corp.values()),
    }

    # ─── 5. ECOS ────────────────────────────────────────────────────
    ecos_root = root / "ecos"
    ecos_files = sorted(ecos_root.glob("*.jsonl")) if ecos_root.exists() else []
    report["ecos"] = {
        "series_files": len(ecos_files),
        "files": [{"name": p.stem, "rows": _count_lines(p)} for p in ecos_files],
    }

    # ─── 출력 ──────────────────────────────────────────────────────
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"FinGraph data 인벤토리 — root = {root}")

    _section("DART 마스터 (corp_codes)")
    dm = report["dart_master"]
    print(f"  zip       : {'✓' if dm['zip_exists'] else '✗'} ({_size_human(dm['zip_size'])})")
    print(f"  상장사    : {dm['listed_count']:,} 건")

    _section("KRX 마스터")
    print(f"  KOSPI 상위 200  : {report['krx']['top_kospi_200']:,}")
    print(f"  KOSDAQ 상위 100 : {report['krx']['top_kosdaq_100']:,}")

    _section("Targets (DART × KRX 매칭)")
    print(f"  ingest_targets : {len(targets):,}")
    for mkt, ts in sorted(target_market.items()):
        print(f"    - {mkt:8s}: {len(ts):,}")

    _section("DART Bulk (회사별)")
    db = report["dart_bulk"]
    print(f"  target_total   : {db['target_total']:,}")
    print(f"  ✓ complete     : {db['complete']:,}")
    print(f"  · partial      : {db['partial']:,}")
    print(f"  ✗ not_started  : {db['not_started']:,}")
    print(f"  expected_years : {db['expected_years']}")
    print(f"  finstat rows   : {db['total_finstat_rows']:,}")

    if partial[:5]:
        print(f"  partial 샘플 (앞 5):")
        for cc in partial[:5]:
            d = per_corp[cc]
            print(f"    - {cc}  company={d['company']}  "
                  f"filings={d['filings_rows']}  "
                  f"years_have={d['years_present']}  "
                  f"missing={d['years_missing']}  "
                  f"empty={d['years_empty']}")

    if not_started[:5]:
        print(f"  not_started 샘플: {not_started[:5]}")

    if extras:
        print(f"  ⚠ extras (targets 외 corp): {len(extras)} — {extras[:5]}")

    _section("ECOS")
    if not ecos_files:
        print("  (없음 — ECOS_API_KEY 설정 후 `make ingest-ecos`)")
    else:
        for item in report["ecos"]["files"]:
            print(f"  - {item['name']:20s} rows={item['rows']:,}")

    # ─── 종합 ──────────────────────────────────────────────────────
    _section("종합")
    issues = []
    if not dm["zip_exists"]:
        issues.append("DART corp_codes 미수집 → `make ingest-corp`")
    if not krx_kospi.exists():
        issues.append("KRX top 미수집 → `make ingest-krx`")
    if not targets_path.exists():
        issues.append("Targets 미생성 → `python scripts/ingest/build_targets.py`")
    if db["not_started"] > 0:
        issues.append(f"미시작 회사 {db['not_started']}건 → `python scripts/ingest/bulk_dart.py`")
    if db["partial"] > 0:
        issues.append(f"불완전 회사 {db['partial']}건 → `python scripts/ingest/bulk_dart.py --retry-failed` 또는 재실행")
    if not ecos_files:
        issues.append("ECOS 미수집 (선택)")

    if issues:
        print(f"  발견된 이슈 {len(issues)}건:")
        for i, msg in enumerate(issues, 1):
            print(f"   {i}. {msg}")
        return 1
    print("  ✓ 모든 검증 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
