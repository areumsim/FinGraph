"""한국은행 ECOS 거시지표 다운로드.

사용:
    # 사전 정의 지표 일괄 (3년치)
    python scripts/ingest/download_ecos.py

    # 특정 지표 + 기간
    python scripts/ingest/download_ecos.py --names base_rate,usd_krw \
        --start 20220101 --end 20241231
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.config import get_settings  # noqa: E402
from autonexusgraph.ingestion.ecos_client import KEY_STATS, EcosClient  # noqa: E402


def _format_date_for_cycle(d: date, cycle: str) -> str:
    if cycle == "D":
        return d.strftime("%Y%m%d")
    if cycle == "M":
        return d.strftime("%Y%m")
    if cycle == "Q":
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"
    if cycle == "A":
        return str(d.year)
    return d.strftime("%Y%m%d")


def main() -> int:
    parser = argparse.ArgumentParser(description="ECOS 거시지표 다운로드")
    parser.add_argument("--names", type=str, default=",".join(KEY_STATS.keys()),
                        help=f"지표 별칭 (쉼표 구분). 가능한 값: {list(KEY_STATS.keys())}")
    parser.add_argument("--start", type=str, default=None,
                        help="시작 (YYYYMMDD/YYYYMM 자동 변환). 기본: 3년 전")
    parser.add_argument("--end", type=str, default=None, help="종료 (자동). 기본: 오늘")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="기본: data/raw/ecos")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    out_dir = args.out_dir or (s.ingest_raw_dir / "ecos")

    today = date.today()
    start_default = date(today.year - s.ingest_years_back, 1, 1)

    names = [n.strip() for n in args.names.split(",") if n.strip()]
    print(f"[INFO] names={names} → {out_dir}")

    if args.dry_run:
        print("[DRY] 실행 안 함")
        return 0

    if not s.ecos_api_key:
        print("[ERROR] ECOS_API_KEY 미설정. ecos.bok.or.kr 에서 키 발급 후 .env 에 추가.",
              file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    with EcosClient(api_key=s.ecos_api_key) as client:
        for name in names:
            if name not in KEY_STATS:
                print(f"[WARN] unknown stat: {name}", file=sys.stderr)
                continue
            meta = KEY_STATS[name]
            cycle = meta["cycle"]
            start = args.start or _format_date_for_cycle(start_default, cycle)
            end = args.end or _format_date_for_cycle(today, cycle)
            try:
                rows = client.get_statistic(
                    stat_code=meta["stat_code"], start=start, end=end,
                    cycle=cycle, item_code1=meta["item"],
                )
            except Exception as e:
                print(f"[WARN] {name} 실패: {e}", file=sys.stderr)
                continue
            out_path = out_dir / f"{name}.jsonl"
            with out_path.open("w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")
            print(f"[OK] {name}: {len(rows):,} rows → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
