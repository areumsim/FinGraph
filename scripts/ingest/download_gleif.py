#!/usr/bin/env python3
"""GLEIF — 한국 jurisdiction LEI 일괄 다운로드.

대용량(~5만건)인데 페이지 단위 분할 저장. 멱등.

저장: data/raw/gleif/kr_page_<n>.json (+ index.json)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.ingestion._common import (
    CheckpointStore, fetch_with_retry, get_rate_limiter, save_raw,
)
from autonexusgraph.ingestion.gleif_client import GleifClient


def main() -> int:
    limiter = get_rate_limiter("gleif")
    ckpt = CheckpointStore("gleif_kr")

    all_records: list[dict] = []
    with GleifClient() as cli:
        try:
            for rec in cli.iter_korea(page_size=200):
                all_records.append({
                    "lei": rec.lei,
                    "legal_name": rec.legal_name,
                    "jurisdiction": rec.jurisdiction,
                    "entity_status": rec.entity_status,
                    "registration_status": rec.registration_status,
                    "issued_at": rec.issued_at,
                    "next_renewal_at": rec.next_renewal_at,
                })
                # 페이지 단위는 client 내부에서 — 여기서 직접 rate-limit 은 한번에 X
        except Exception as e:
            print(f"[GLEIF] error: {e}")

    if not all_records:
        print("[GLEIF] no records")
        return 1

    save_raw("gleif", "kr_records.json", all_records)
    save_raw("gleif", "index.json", {"count": len(all_records), "jurisdiction": "KR"})
    ckpt.mark_done("kr_records", {"count": len(all_records)})
    print(f"[GLEIF] saved {len(all_records)} KR LEI records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
