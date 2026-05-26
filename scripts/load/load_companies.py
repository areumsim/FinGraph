"""master.companies 적재 진입점."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fingraph.loaders import load_companies  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="master.companies 적재 (DART + KRX)")
    parser.add_argument("--dry-run", action="store_true",
                        help="SQL 만 출력하고 실행 X")
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    stats = load_companies(dry_run=args.dry_run, batch_size=args.batch_size)
    print(f"[OK] companies — {stats.summary()}")
    if args.dry_run:
        for line in stats.sql_preview:
            print(line)
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
