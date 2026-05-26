"""DART 정형 지배구조 → Neo4j 적재.

선행: scripts/ingest/bulk_dart_structural.py 로 raw 수집 완료.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fingraph.loaders import (  # noqa: E402
    load_executives, load_shareholders, load_subsidiaries,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Neo4j 지배구조 적재")
    parser.add_argument("--apis", type=str, default="subsidiaries,executives,shareholders",
                        help="쉼표 구분")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    apis = {a.strip() for a in args.apis.split(",")}
    if "subsidiaries" in apis:
        s = load_subsidiaries(dry_run=args.dry_run)
        print(f"[OK] subsidiaries  — {s.summary()}")
    if "executives" in apis:
        s = load_executives(dry_run=args.dry_run)
        print(f"[OK] executives    — {s.summary()}")
    if "shareholders" in apis:
        s = load_shareholders(dry_run=args.dry_run)
        print(f"[OK] shareholders  — {s.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
