"""NHTSA ODI Investigations — 결함 조사 (리콜 전단계) bulk flat-file 수집.

다운로드 (키 불필요):
    https://static.nhtsa.gov/odi/ffdd/inv/FLAT_INV.zip       (~4 MB, daily 갱신)
    https://static.nhtsa.gov/odi/ffdd/inv/INV.txt            (필드 정의 — 캐시 보존)

FLAT_INV.zip 의 안에는 ``FLAT_INV.txt`` 가 들어있고 TAB-delimited (no header):

    1  NHTSA_ACTION_NUMBER  (10, 'PE12001' / 'EA22002' / 'RQ23003' …)
    2  MAKE                 (25)
    3  MODEL                (256)
    4  YEAR                 (4, '9999'=unknown)
    5  COMPNAME             (256, 부품 자유 텍스트)
    6  MFR_NAME             (40)
    7  ODATE                (8, YYYYMMDD — 조사 개시)
    8  CDATE                (8, YYYYMMDD — 조사 종결, 진행 중이면 빈값)
    9  CAMPNO               (9, 리콜로 종결됐을 때 그 캠페인 #)
    10 SUBJECT              (200)
    11 SUMMARY              (6000)

본 모듈은 zip → ``data/raw/auto/nhtsa_investigations/FLAT_INV.zip`` 보존.
loader (load_auto_investigations) 가 unzip + 파싱 + variant 매칭 + PG/Neo4j 적재.

PRD §3.5 등급: A (0.95) — NHTSA 공식.

CLI:
    python -m autograph.ingestion.nhtsa_investigations
    python -m autograph.ingestion.nhtsa_investigations --no-cache    # 갱신 강제
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import httpx

from autonexusgraph.config import get_settings
from autonexusgraph.ingestion._common import (
    CheckpointStore,
    RateLimiter,
    fetch_with_retry,
)


log = logging.getLogger(__name__)


_LIMITER = RateLimiter(per_sec=0.5)        # bulk file — 매우 보수적.
_SOURCE = "auto/nhtsa_investigations"
_USER_AGENT = "AutoGraph-Research/0.1 (ifkbn@kolon.com)"

FLAT_INV_URL = "https://static.nhtsa.gov/odi/ffdd/inv/FLAT_INV.zip"
INV_DICT_URL = "https://static.nhtsa.gov/odi/ffdd/inv/INV.txt"


def _raw_root() -> Path:
    root = get_settings().ingest_raw_dir / "auto" / "nhtsa_investigations"
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


def fetch_flat_inv(*, force: bool = False) -> Path:
    """FLAT_INV.zip 다운로드. checkpoint 로 멱등성. force=True 면 캐시 무시."""
    dest = _raw_root() / "FLAT_INV.zip"
    ckpt = CheckpointStore(_SOURCE)
    key = "FLAT_INV.zip"

    if not force and dest.exists() and ckpt.is_done(key):
        log.info("[inv] skip — 캐시 hit %s", dest)
        ckpt.mark_skipped()
        return dest

    try:
        size = _download(FLAT_INV_URL, dest)
        log.info("[inv] downloaded %s (%.1f MB)", dest, size / 1024 / 1024)
        ckpt.mark_done(key, {"size": size, "url": FLAT_INV_URL})
    except Exception as e:  # noqa: BLE001
        log.exception("[inv] download failed")
        ckpt.mark_failed(key, str(e))
        raise

    # 데이터 사전도 함께 캐시 (감사용, 최초 1회 OK 면 skip).
    dict_path = _raw_root() / "INV.txt"
    if force or not dict_path.exists():
        try:
            _download(INV_DICT_URL, dict_path)
            log.info("[inv] cached data dictionary %s", dict_path)
        except Exception as e:  # noqa: BLE001
            log.warning("[inv] dictionary fetch failed (skip): %s", e)

    return dest


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.nhtsa_investigations")
    ap.add_argument("--no-cache", action="store_true",
                    help="캐시 무시 — 항상 재다운로드")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    fetch_flat_inv(force=args.no_cache)


if __name__ == "__main__":
    main()


__all__ = ["fetch_flat_inv", "FLAT_INV_URL", "INV_DICT_URL"]
