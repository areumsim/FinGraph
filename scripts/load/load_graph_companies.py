"""master.companies → Neo4j (Company/Market/Sector/Person)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.loaders import load_graph_companies  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Neo4j Company 노드 + 시장/섹터/CEO 적재")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()

    stats = load_graph_companies(dry_run=args.dry_run, batch_size=args.batch_size)
    print(f"[OK] neo4j companies — {stats.summary()}")
    if args.dry_run:
        for line in stats.sql_preview:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
