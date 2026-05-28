"""embedding IS NULL 인 vec.chunks 행에 BGE-M3 임베딩 채우기.

사전 요건:
    python scripts/serve_embeddings.py   # 다른 터미널에서

사용:
    python scripts/load/embed_chunks.py
    python scripts/load/embed_chunks.py --batch-size 32
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.loaders import embed_chunks  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="vec.chunks 임베딩 backfill (BGE-M3)")
    parser.add_argument("--batch-size", type=int, default=64,
                        help="BGE-M3 호출 1회당 청크 수")
    parser.add_argument("--no-progress", action="store_true")
    args = parser.parse_args()

    try:
        stats = embed_chunks(batch_size=args.batch_size, progress=not args.no_progress)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    print(f"[OK] embedded — {stats.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
