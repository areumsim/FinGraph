"""data.go.kr 15155857 — 자동차검사관리 수리검사내역 (파일 다운).

원본은 파일 다운로드 형식 (Excel/CSV). 본 모듈은 raw 파일이
``data/raw/datagokr/inspections/*.csv`` 에 존재하면 normalize 만 수행.
파일 미존재 시 graceful skip — 다운 가이드를 로그로 안내.

CLI:
    python -m autograph.ingestion.datagokr_inspections

선행:
    1. https://www.data.go.kr/data/15155857/fileData.do 에서 CSV 다운
    2. ``data/raw/datagokr/inspections/<year>.csv`` 로 저장
    3. 본 명령 실행 — `data/raw/auto/datagokr_inspections/<year>.jsonl` 로 normalize
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from ..config import get_auto_settings


log = logging.getLogger(__name__)


def _normalize_row(raw: dict) -> dict:
    """KOTSA CSV row → 통일 schema."""
    return {
        "inspection_id":  raw.get("검사번호") or raw.get("inspection_id"),
        "vin":            raw.get("차대번호") or raw.get("vin"),
        "make_kr":        raw.get("제작사") or raw.get("make"),
        "model_kr":       raw.get("차종") or raw.get("model"),
        "year":           raw.get("연식") or raw.get("year"),
        "inspection_type": raw.get("검사종류") or raw.get("type"),
        "result":         raw.get("판정") or raw.get("result"),
        "inspected_at":   raw.get("검사일자") or raw.get("inspected_at"),
        "reason":         raw.get("사유") or raw.get("reason"),
    }


def run() -> int:
    s = get_auto_settings()
    src_dir = s.datagokr_kotsa_inspection_dir / "inspections"
    if not src_dir.exists() or not any(src_dir.glob("*.csv")):
        log.warning(
            "[datagokr_inspections] %s 에 CSV 없음 — "
            "https://www.data.go.kr/data/15155857/fileData.do 에서 다운 후 재실행",
            src_dir,
        )
        return 0

    out_root = Path(get_auto_settings().datagokr_kotsa_inspection_dir).parent / "auto" / "datagokr_inspections"
    out_root.mkdir(parents=True, exist_ok=True)

    total = 0
    for csv_path in sorted(src_dir.glob("*.csv")):
        year = csv_path.stem
        out_path = out_root / f"{year}.jsonl"
        n_rows = 0
        with csv_path.open(encoding="utf-8-sig") as fi, \
                out_path.open("w", encoding="utf-8") as fo:
            reader = csv.DictReader(fi)
            for row in reader:
                normalized = _normalize_row(row)
                fo.write(json.dumps(normalized, ensure_ascii=False) + "\n")
                n_rows += 1
        log.info("[datagokr_inspections] %s → %s (rows=%d)",
                 csv_path.name, out_path.name, n_rows)
        total += n_rows

    log.info("[datagokr_inspections] 완료. 누적 rows=%d", total)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run"]
