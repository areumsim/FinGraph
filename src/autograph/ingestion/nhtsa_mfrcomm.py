"""NHTSA Manufacturer Communications (TSB 포함) — manual-file 모드.

상태: 자동 다운로드 불가 (2026-05 기준)
    NHTSA 가 `https://static.nhtsa.gov/odi/ffdd/tsbs/FLAT_TSBS.zip` URL 을 retire
    한 뒤 새 위치를 공식적으로 공지하지 않았고, 봇 차단으로 file-downloads 페이지에서
    자동 추출도 불가. data.transportation.gov 의 dataset (id: fmyn-qyh5) 은
    "non-tabular" 로 표시되어 Socrata SODA 도 거부.

사용자 액션:
    1) https://www.nhtsa.gov/nhtsa-datasets-and-apis 또는 file-downloads 에서
       "Manufacturer Communications Flat File" 또는 FLAT_TSBS.zip 을 직접 다운.
    2) ``data/raw/auto/nhtsa_mfrcomm/FLAT_TSBS.zip`` 에 그대로 배치.
    3) ``python -m autograph.loaders.load_auto_mfrcomm`` 실행.

스키마 (static.nhtsa.gov/odi/ffdd/tsbs/TSBS.txt 에서 확인됨, TAB-delimited 14 컬럼):

    1  NHTSA_ID_NUMBER                  NUMBER 9
    2  REPLACEMENT_SERVICE_BULLETIN_NO  CHAR   16
    3  DATE_ADDED_TO_FILE               DATE   8 (YYYYMMDD)
    4  TSB_DOCUMENT_ID                  CHAR   128
    5  MFR_COMMUNICATION_DATE           DATE   8
    6  MFR_INTERNAL_CAMPAIGN_ID         CHAR   128
    7  COMMUNICATION_TYPE               CHAR   40  (Service Bulletin / Campaign /
                                                     Warranty / OTA / Emissions / Other)
    8  MAKE                             CHAR   128
    9  MODEL                            CHAR   256
    10 MODEL_YEAR                       CHAR   4   (9999=unknown)
    11 NHTSA_COMPONENTS                 CHAR   256 (comma-sep up to 5)
    12 MFR_COMPONENT_SYSTEM             CHAR   256
    13 MFR_COMPONENT_SUBSYSTEM          CHAR   256
    14 SUMMARY                          CHAR   4000

`fetch_flat_tsbs()` 가 호출되면 — 자동 다운로드 시도 X — 사용자에게 안내 출력.

CLI:
    python -m autograph.ingestion.nhtsa_mfrcomm        # 안내 출력
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from autonexusgraph.config import get_settings


log = logging.getLogger(__name__)


_SOURCE = "auto/nhtsa_mfrcomm"

INSTRUCTIONS = """
[nhtsa_mfrcomm] 자동 다운로드 불가 — 다음 절차로 수동 다운:

  1. 브라우저로 https://www.nhtsa.gov/nhtsa-datasets-and-apis 또는
     https://www.nhtsa.gov/file-downloads 방문.
  2. "Manufacturer Communications" / "Technical Service Bulletins" 다운로드 링크
     클릭 → FLAT_TSBS.zip (또는 FLAT_MFRCOMM.zip) 저장.
  3. 다음 경로에 배치:
        {dest}
  4. python -m autograph.loaders.load_auto_mfrcomm 실행.

또는 https://data.transportation.gov/Automobiles/.../fmyn-qyh5 페이지에서 직접
다운로드 (제공 시).
"""


def _raw_root() -> Path:
    root = get_settings().ingest_raw_dir / "auto" / "nhtsa_mfrcomm"
    root.mkdir(parents=True, exist_ok=True)
    return root


def print_instructions() -> Path:
    """수동 다운로드 안내. 목적 경로 반환."""
    dest = _raw_root() / "FLAT_TSBS.zip"
    log.warning(INSTRUCTIONS.format(dest=dest))
    return dest


def fetch_flat_tsbs() -> Path | None:
    """현재는 자동 다운로드 불가 — 안내만 출력 후 None.

    파일이 이미 있으면 그 경로 반환 (idempotent — loader 의 사전 체크용).
    """
    dest = _raw_root() / "FLAT_TSBS.zip"
    alt = _raw_root() / "FLAT_MFRCOMM.zip"
    for p in (dest, alt):
        if p.exists():
            log.info("[mfrcomm] cached %s (%d bytes)", p, p.stat().st_size)
            return p
    print_instructions()
    return None


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.nhtsa_mfrcomm")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    fetch_flat_tsbs()


if __name__ == "__main__":
    main()


__all__ = ["fetch_flat_tsbs", "print_instructions", "INSTRUCTIONS"]
