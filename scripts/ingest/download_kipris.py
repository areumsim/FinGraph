#!/usr/bin/env python3
"""KIPRIS 회사별 특허 출원 — 295 회사 × 최근 5년.

키 필요. 키 없으면 안내 출력.
저장: data/raw/kipris/<corp_code>/<year>.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_pool
from autonexusgraph.ingestion._common import (
    CheckpointStore, fetch_with_retry, get_rate_limiter, save_raw,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--year-from", type=int, default=2020)
    args = parser.parse_args()

    s = get_settings()
    if not s.kipris_api_key:
        print("KIPRIS_API_KEY 미설정 — plus.kipris.or.kr 에서 무료 키 발급 후 .env 추가")
        print("우선 client + 적재 코드 준비됨. 키 확보 후 동일 명령 재실행.")
        return 1

    from autonexusgraph.ingestion.kipris_client import KiprisClient

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT corp_code, corp_name
              FROM master.companies
             WHERE is_active = TRUE
             ORDER BY corp_code
        """)
        targets = cur.fetchall()
    if args.limit:
        targets = targets[:args.limit]
    print(f"[KIPRIS] targets: {len(targets)}")

    limiter = get_rate_limiter("kipris")
    ckpt = CheckpointStore("kipris")

    with KiprisClient(api_key=s.kipris_api_key) as cli:
        for i, (corp_code, name) in enumerate(targets, 1):
            if ckpt.is_done(corp_code):
                continue
            limiter.acquire()
            try:
                data = fetch_with_retry(
                    lambda: cli.search_by_applicant(name, year_from=args.year_from),
                    max_tries=3,
                )
                save_raw("kipris", f"{corp_code}/applicant_{args.year_from}.json", data)
                ckpt.mark_done(corp_code, {"name": name})
                if i % 20 == 0:
                    print(f"  [{i}/{len(targets)}] done={ckpt.stats.done}")
            except Exception as e:
                ckpt.mark_failed(corp_code, str(e))

    print(f"\n[KIPRIS] done={ckpt.stats.done} failed={ckpt.stats.failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
