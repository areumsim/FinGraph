"""DART 사업보고서 zip → 청킹 → vec.chunks 적재 (embedding NULL).

임베딩은 별도 단계 (scripts/load/embed_chunks.py) — BGE-M3 서버 필요.

사용:
    python scripts/load/build_chunks.py
    python scripts/load/build_chunks.py --limit-reports 10   # smoke
    python scripts/load/build_chunks.py --dry-run            # 개수만
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fingraph.loaders import load_chunks  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="DART zip → vec.chunks 적재")
    parser.add_argument("--limit-reports", type=int, default=None,
                        help="처음 N 개 zip 만 (smoke)")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args()

    stats = load_chunks(
        limit_reports=args.limit_reports,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        progress=not args.no_progress,
    )
    print(f"[OK] chunks — {stats.summary()}")
    if args.dry_run:
        for line in stats.sql_preview:
            print(line)
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
