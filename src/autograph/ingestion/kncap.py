"""KNCAP (한국 자동차안전도평가) — 안전등급 수집.

공식 API 채널은 지정 통신 (car.go.kr / KNCAP 사무국). 본 모듈은 두 경로 지원:

1. **API 키 모드** — `KNCAP_API_KEY` 설정 시 HTTP 호출 (endpoint 는 확정 후 채움).
2. **수동 모드** — `data/raw/auto/kncap/*.csv` 또는 `*.json` 가 있으면 normalize.

둘 다 부재 시 graceful skip — exit 0.

CLI:
    python -m autograph.ingestion.kncap
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from autonexusgraph.config import get_settings
from autonexusgraph.ingestion._common import save_raw

from ..config import get_auto_settings


log = logging.getLogger(__name__)


_SOURCE = "auto/kncap"


def _normalize_csv_row(raw: dict) -> dict:
    return {
        "model_year":       raw.get("연식") or raw.get("year"),
        "manufacturer_kr":  raw.get("제조사") or raw.get("manufacturer"),
        "model_kr":         raw.get("차종") or raw.get("model"),
        "overall_rating":   raw.get("종합등급") or raw.get("overall"),
        "frontal_impact":   raw.get("정면충돌") or raw.get("frontal"),
        "side_impact":      raw.get("측면충돌") or raw.get("side"),
        "rollover":         raw.get("전복") or raw.get("rollover"),
        "test_date":        raw.get("평가일자") or raw.get("date"),
        "report_url":       raw.get("보고서URL") or raw.get("url"),
    }


def _api_mode() -> int:
    s = get_auto_settings()
    if not s.kncap_api_key:
        return 0
    # KNCAP 공식 endpoint 가 발급되면 여기에 채움.
    log.warning("[kncap] API endpoint 미확정 — 본 PR 에서는 graceful skip 만 유지")
    return 0


def _file_mode() -> int:
    raw_root = get_settings().ingest_raw_dir / _SOURCE
    raw_root.mkdir(parents=True, exist_ok=True)

    csvs = list(raw_root.glob("*.csv"))
    jsons = list(raw_root.glob("*.json"))
    if not csvs and not jsons:
        log.warning(
            "[kncap] %s 에 raw 파일 없음 — KNCAP 사무국 채널로 받은 자료를 본 경로에 두세요.",
            raw_root,
        )
        return 0

    total = 0
    for csv_path in sorted(csvs):
        out_path = csv_path.with_suffix(".jsonl")
        n_rows = 0
        with csv_path.open(encoding="utf-8-sig") as fi, \
                out_path.open("w", encoding="utf-8") as fo:
            reader = csv.DictReader(fi)
            for row in reader:
                fo.write(json.dumps(_normalize_csv_row(row), ensure_ascii=False) + "\n")
                n_rows += 1
        log.info("[kncap] %s → %s (rows=%d)", csv_path.name, out_path.name, n_rows)
        total += n_rows

    for json_path in sorted(jsons):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("[kncap] %s 파싱 실패: %s", json_path.name, exc)
            continue
        items = payload.get("data") or payload.get("items") or payload
        if isinstance(items, list):
            log.info("[kncap] %s rows=%d", json_path.name, len(items))
            total += len(items)

    log.info("[kncap] 완료. 누적 rows=%d", total)
    return total


def run() -> int:
    n = _api_mode()
    if n > 0:
        return n
    return _file_mode()


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
