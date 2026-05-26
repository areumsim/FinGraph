"""fin.financials 적재 진입점 (184K+ rows)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fingraph.loaders import load_financials  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="fin.financials 적재 (XBRL)")
    parser.add_argument("--dry-run", action="store_true",
                        help="row 수 / 배치 수만 계산하고 적재 안 함")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--no-progress", action="store_true",
                        help="tqdm 비활성")
    args = parser.parse_args()

    stats = load_financials(
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        progress=not args.no_progress,
    )
    print(f"[OK] financials — {stats.summary()}")
    if args.dry_run:
        for line in stats.sql_preview:
            print(line)
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
