#!/usr/bin/env python3
"""공정거래위원회 대규모기업집단 — data.go.kr API 또는 수동 CSV.

선호: data.go.kr API (`DATA_GO_KR_API_KEY`). 미설정 시 manual CSV 가이드 출력.

수동 fallback:
- 공정위 공시정보시스템 (opni.ftc.go.kr) 에서 직접 다운로드
- 또는 https://www.ftc.go.kr 보도자료의 매년 5월 지정 결과 CSV
- 저장 위치: data/raw/ftc/<year>/groups.csv

사용:
    python scripts/ingest/download_ftc_groups.py --year 2024
    python scripts/ingest/download_ftc_groups.py --years 2022,2023,2024
    python scripts/ingest/download_ftc_groups.py --manual-only  # 가이드만
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.ingestion._common import save_raw
from autonexusgraph.ingestion.ftc_client import FtcClient


MANUAL_GUIDE = """
[수동 다운로드 가이드 — FTC 기업집단]

1) data.go.kr 무료 API 키 발급 → .env 의 DATA_GO_KR_API_KEY 에 입력
   • https://www.data.go.kr/data/15083033/openapi.do
2) 또는 공정위 공시정보시스템에서 CSV 다운로드
   • https://opni.ftc.go.kr — '대규모기업집단현황' 메뉴 → 연도별
   • 저장: data/raw/ftc/<year>/groups.csv
3) 또는 FTC 보도자료 (매년 5월 지정 결과)
   • https://www.ftc.go.kr → '대규모기업집단' → 지정현황 HWP/CSV 다운로드
   • 저장 위치는 동일

저장 후: python scripts/load/load_ftc_groups.py 실행
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--years", type=str, default=None,
                        help="쉼표 구분, 예: 2022,2023,2024")
    parser.add_argument("--manual-only", action="store_true")
    args = parser.parse_args()

    if args.manual_only:
        print(MANUAL_GUIDE)
        return 0

    s = get_settings()
    api_key = s.data_go_kr_api_key
    if not api_key:
        print("[FTC] DATA_GO_KR_API_KEY 미설정.")
        print(MANUAL_GUIDE)
        # 수동 CSV 가 이미 있으면 raw 로 복사
        manual_root = Path("data/raw/ftc")
        if manual_root.exists():
            csvs = list(manual_root.rglob("*.csv"))
            if csvs:
                print(f"[FTC] 수동 CSV 발견: {len(csvs)}개 — load_ftc_groups.py 로 적재 가능")
        return 0

    if args.years:
        years = [int(y) for y in args.years.split(",")]
    else:
        years = [args.year] if args.year else [2024]

    with FtcClient(api_key=api_key) as cli:
        for year in years:
            try:
                rows = cli.fetch_groups(year)
            except Exception as e:
                print(f"[FTC] {year} 실패: {e}")
                continue
            save_raw("ftc", f"{year}/groups.json", rows)
            print(f"[FTC] {year}: {len(rows)} rows → data/raw/ftc/{year}/groups.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
