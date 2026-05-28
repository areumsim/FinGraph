"""전체 적재 — companies → filings → financials 순.

순서가 중요한 이유: filings/financials 에 corp_code FK 가 master.companies 를 참조.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.loaders import load_companies, load_filings, load_financials  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="전체 PG 적재 (순차)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip", type=str, default="",
                        help="companies/filings/financials 쉼표 구분으로 스킵")
    args = parser.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    total_failed = 0

    if "companies" not in skip:
        s = load_companies(dry_run=args.dry_run)
        print(f"[OK] companies  — {s.summary()}")
        total_failed += s.failed
    if "filings" not in skip:
        s = load_filings(dry_run=args.dry_run)
        print(f"[OK] filings    — {s.summary()}")
        total_failed += s.failed
    if "financials" not in skip:
        s = load_financials(dry_run=args.dry_run)
        print(f"[OK] financials — {s.summary()}")
        total_failed += s.failed

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
