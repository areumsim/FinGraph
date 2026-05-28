#!/usr/bin/env python3
"""SEC EDGAR — 한국 ADR/외국법인의 SEC 공시.

대상 선정:
- master.entity_map 에 id_type='cik' 이 있는 회사 (Wikidata 수집 시 P5531 매핑됨)
- 없으면 manual CIK 리스트도 인자로 전달 가능

저장:
  data/raw/sec/<cik>/submissions.json
  data/raw/sec/<cik>/companyfacts.json

사용:
    python scripts/ingest/download_sec_edgar.py
    python scripts/ingest/download_sec_edgar.py --cik 1325258,1003415
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
from autonexusgraph.ingestion.sec_client import SecEdgarClient


SELECT_CIK = """
SELECT em.corp_code, em.id_value AS cik, c.corp_name
  FROM master.entity_map em
  JOIN master.companies c ON c.corp_code = em.corp_code
 WHERE em.id_type = 'cik' AND c.is_active = TRUE
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cik", default=None, help="쉼표 구분 — 자동 매핑 무시")
    parser.add_argument("--with-facts", action="store_true",
                        help="companyfacts (큰 파일) 도 같이 받기")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    s = get_settings()

    if args.cik:
        targets = [(None, c.strip(), None) for c in args.cik.split(",") if c.strip()]
    else:
        pool = get_pool()
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(SELECT_CIK)
            targets = cur.fetchall()

    print(f"[SEC] targets: {len(targets)}")
    if not targets:
        print("  매핑된 CIK 없음. Wikidata 적재 후 다시 시도하거나 --cik 로 직접 지정.")
        return 0

    ckpt = CheckpointStore("sec_edgar")
    limiter = get_rate_limiter("sec_edgar")

    with SecEdgarClient(user_agent=s.sec_user_agent) as cli:
        for corp_code, cik, name in targets:
            entity_id = str(cik).zfill(10)
            if ckpt.is_done(entity_id) and not args.force:
                continue
            limiter.acquire()
            print(f"[SEC] cik={cik} corp_code={corp_code} name={name}")
            try:
                sub = fetch_with_retry(lambda: cli.get_submissions(cik), max_tries=3)
                if not sub:
                    ckpt.mark_failed(entity_id, "submissions_404")
                    continue
                save_raw("sec", f"{entity_id}/submissions.json", sub)

                if args.with_facts:
                    limiter.acquire()
                    facts = fetch_with_retry(lambda: cli.get_company_facts(cik), max_tries=3)
                    if facts:
                        save_raw("sec", f"{entity_id}/companyfacts.json", facts)

                filings = cli.extract_filings(sub)
                print(f"   filings: {len(filings)}")
                ckpt.mark_done(entity_id, {"corp_code": corp_code, "filings": len(filings)})
            except Exception as e:
                ckpt.mark_failed(entity_id, str(e))
                print(f"   failed: {e}")

    print(f"\n[SEC] done={ckpt.stats.done} failed={ckpt.stats.failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
