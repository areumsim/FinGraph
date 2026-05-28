#!/usr/bin/env python3
"""KOSIS 산업/거시 통계 수집.

기본 시계열 set (운영 단계에서 확장):
- 광업제조업동향조사 (생산/출하/재고)
- 한국은행 기준금리
- 환율, 소비자물가지수
- 산업별 매출액

각 시계열 raw → data/raw/kosis/<stat_code>/<period>.json

사용:
    python scripts/ingest/download_kosis.py [--start 2020 --end 2026]
    python scripts/ingest/download_kosis.py --series rate_base    # 기준금리만
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.ingestion._common import (
    CheckpointStore, fetch_with_retry, get_rate_limiter, save_raw,
)


# 시계열 정의 — (org_id, tbl_id, period_type, label)
SERIES = {
    "manufacturing": ("101", "DT_1F31013S", "M", "광업제조업동향조사 - 생산지수"),
    # 추가는 KOSIS 검색해서 tbl_id 확인 후 정의
    # "rate_base":  ("301", "DT_039Y001", "M", "한국은행 기준금리"),
    # "cpi":        ("101", "DT_1J17001",  "M", "소비자물가지수"),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", default=",".join(SERIES))
    parser.add_argument("--start", default="2021")
    parser.add_argument("--end",   default="2026")
    args = parser.parse_args()

    s = get_settings()
    if not s.kosis_api_key:
        print("KOSIS_API_KEY 미설정 — kosis.kr/openapi 에서 무료 키 발급 후 .env 추가")
        print("우선 스크립트만 준비됨. 키 확보 후 동일 명령 재실행.")
        return 1

    from autonexusgraph.ingestion.kosis_client import KosisClient  # 키 있을 때만 import

    wanted = {x.strip() for x in args.series.split(",") if x.strip() in SERIES}
    if not wanted:
        print(f"valid series 0개. 가능: {list(SERIES)}")
        return 2

    limiter = get_rate_limiter("kosis")
    ckpt = CheckpointStore("kosis")

    with KosisClient(api_key=s.kosis_api_key) as cli:
        for key in wanted:
            org, tbl, prd, label = SERIES[key]
            entity_id = f"{org}_{tbl}_{args.start}-{args.end}"
            if ckpt.is_done(entity_id):
                continue
            limiter.acquire()
            print(f"[KOSIS] {key} ({label})")
            try:
                # 기간 형식: 'A' → YYYY, 'M' → YYYYMM
                if prd == "M":
                    s_p, e_p = f"{args.start}01", f"{args.end}12"
                elif prd == "Q":
                    s_p, e_p = f"{args.start}01", f"{args.end}04"
                else:
                    s_p, e_p = args.start, args.end
                rows = fetch_with_retry(
                    lambda: cli.fetch_series(org, tbl, prd, s_p, e_p),
                    max_tries=3,
                )
                save_raw("kosis", f"{tbl}/{s_p}-{e_p}.json", rows)
                ckpt.mark_done(entity_id, {"rows": len(rows) if isinstance(rows, list) else 0})
                print(f"   rows: {len(rows) if isinstance(rows, list) else '?'}")
            except Exception as e:
                ckpt.mark_failed(entity_id, str(e))
                print(f"   failed: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
