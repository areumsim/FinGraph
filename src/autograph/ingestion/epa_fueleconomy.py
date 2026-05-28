"""EPA fueleconomy.gov — 차량 연비·엔진·배출 spec bulk CSV 수집.

다운로드 (키 불필요):
    https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip
        → 1984~현재 US 인증 차량 전체 (~45k rows). 단일 CSV (vehicles.csv).
    https://www.fueleconomy.gov/feg/epadata/vehicles.csv          # 미압축 직링크

본 모듈은 zip 우선 다운로드 → ``data/raw/auto/epa_fueleconomy/vehicles.csv.zip`` 보존.
loader (load_auto_epa) 가 unzip + 파싱 + variant 매칭 + spec_measurements 적재.

PRD §3.5 등급: A (0.95) — EPA 인증 공식 출처.

CLI:
    python -m autograph.ingestion.epa_fueleconomy
    python -m autograph.ingestion.epa_fueleconomy --no-cache    # 강제 재다운로드
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

import httpx

from autonexusgraph.config import get_settings
from autonexusgraph.ingestion._common import (
    CheckpointStore,
    RateLimiter,
    fetch_with_retry,
)


log = logging.getLogger(__name__)


# EPA 다운로드 — 자주 호출하지 않음 (bulk file). 매우 보수적 rate.
_LIMITER = RateLimiter(per_sec=0.5)
_SOURCE = "auto/epa_fueleconomy"
_USER_AGENT = "AutoGraph-Research/0.1 (ifkbn@kolon.com)"

EPA_VEHICLES_ZIP_URL = "https://www.fueleconomy.gov/feg/epadata/vehicles.csv.zip"
EPA_VEHICLES_CSV_URL = "https://www.fueleconomy.gov/feg/epadata/vehicles.csv"


def _raw_root() -> Path:
    root = get_settings().ingest_raw_dir / "auto" / "epa_fueleconomy"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _download(url: str, dest: Path, *, timeout: float = 120.0) -> int:
    """url → dest 원자적 다운로드. .tmp 에 쓴 뒤 rename. 바이트 수 반환."""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    headers = {"User-Agent": _USER_AGENT}

    def _do() -> int:
        with httpx.Client(timeout=timeout, headers=headers,
                          follow_redirects=True) as client:
            with client.stream("GET", url) as r:
                r.raise_for_status()
                size = 0
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)
                            size += len(chunk)
                return size

    _LIMITER.acquire()
    size = fetch_with_retry(_do, max_tries=3, base=4.0)
    tmp.replace(dest)
    return size


def fetch_vehicles_zip(*, force: bool = False) -> Path:
    """vehicles.csv.zip 을 다운로드 (or 캐시 hit). checkpoint 로 멱등성 보장.

    force=True 면 캐시 무시하고 재다운로드 (zip 갱신될 때 사용).
    """
    dest = _raw_root() / "vehicles.csv.zip"
    ckpt = CheckpointStore(_SOURCE)
    key = "vehicles.csv.zip"

    if not force and dest.exists() and ckpt.is_done(key):
        log.info("[epa] skip — 캐시 hit %s", dest)
        ckpt.mark_skipped()
        return dest

    try:
        size = _download(EPA_VEHICLES_ZIP_URL, dest)
        log.info("[epa] downloaded %s (%.1f MB)", dest, size / 1024 / 1024)
        ckpt.mark_done(key, {"size": size, "url": EPA_VEHICLES_ZIP_URL})
        return dest
    except Exception as e:  # noqa: BLE001
        log.exception("[epa] download failed")
        ckpt.mark_failed(key, str(e))
        raise


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.epa_fueleconomy")
    ap.add_argument("--no-cache", action="store_true",
                    help="캐시 무시 — 항상 재다운로드 (EPA 갱신 반영)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    fetch_vehicles_zip(force=args.no_cache)


if __name__ == "__main__":
    main()


__all__ = ["fetch_vehicles_zip", "EPA_VEHICLES_ZIP_URL", "EPA_VEHICLES_CSV_URL"]
