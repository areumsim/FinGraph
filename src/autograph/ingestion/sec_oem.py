"""SEC EDGAR Company Facts — 글로벌 OEM 공시 재무 수집 (키 불필요).

API:
    https://data.sec.gov/api/xbrl/companyfacts/CIK{10-digit-zero-padded}.json

PRD §3.5 등급: A (0.95) — SEC 공식 XBRL.

Rate: SEC 정책 10 req/sec 글로벌. User-Agent 에 contact email 필수.

OEM CIK 시드 (Hyundai/Kia/Genesis/KGM 등 한국 OEM 은 SEC 미발행 → DART 측에서 처리):

    Tesla              1318605    10-K
    Ford               37996      10-K
    General Motors     1467858    10-K
    Stellantis         1605484    20-F (네덜란드 상장 ADR)
    Toyota Motor ADR   1094517    20-F
    Honda Motor ADR    715153     20-F
    Rivian             1874178    10-K
    Lucid Group        1811210    10-K
    Nikola             1731289    10-K
    Polestar           1812148    20-F
    Magna International 1019975   40-F (Tier1 부품사 — 캐나다 cross-list)
    Aptiv              1521332    10-K (ADAS Tier1)

저장:
    data/raw/auto/sec_oem/CIK{10}.json
    data/raw/auto/sec_oem/submissions/CIK{10}.json (선택 — submissions 메타)

본 모듈은 companyfacts 만 우선. submissions (10-K 본문 URL) 는 별도 ingest_submissions().

CLI:
    python -m autograph.ingestion.sec_oem
    python -m autograph.ingestion.sec_oem --cik 1318605    # Tesla 만
    python -m autograph.ingestion.sec_oem --include-submissions
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from autonexusgraph.config import get_settings
from autonexusgraph.ingestion._common import (
    CheckpointStore,
    RateLimiter,
    save_raw,
)
from autonexusgraph.ingestion.sec_client import SecEdgarClient


log = logging.getLogger(__name__)


_LIMITER = RateLimiter(per_sec=5.0)    # SEC 정책 10/sec → 안전하게 5.
_SOURCE = "auto/sec_oem"
# SEC 정책: 'Company Name <contact-email>' 패턴.
_USER_AGENT = "AutoGraph-Research ifkbn@kolon.com"


# (cik, company_name, ticker, form_type, country) — 시드.
OEM_SEED: list[tuple[int, str, str, str, str]] = [
    (1318605, "Tesla, Inc.",                       "TSLA",  "10-K", "US"),
    (37996,   "Ford Motor Company",                "F",     "10-K", "US"),
    (1467858, "General Motors Company",            "GM",    "10-K", "US"),
    (1605484, "Stellantis N.V.",                   "STLA",  "20-F", "NL"),
    (1094517, "Toyota Motor Corporation",          "TM",    "20-F", "JP"),
    (715153,  "Honda Motor Co., Ltd.",             "HMC",   "20-F", "JP"),
    (1874178, "Rivian Automotive, Inc.",           "RIVN",  "10-K", "US"),
    (1811210, "Lucid Group, Inc.",                 "LCID",  "10-K", "US"),
    (1731289, "Nikola Corporation",                "NKLA",  "10-K", "US"),
    (1812148, "Polestar Automotive Holding UK PLC", "PSNY", "20-F", "GB"),
    (1019975, "Magna International Inc.",          "MGA",   "40-F", "CA"),  # Tier1 부품사
    (1521332, "Aptiv PLC",                          "APTV", "10-K", "IE"),  # ADAS Tier1
]


def _raw_root() -> Path:
    root = get_settings().ingest_raw_dir / "auto" / "sec_oem"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cik10(cik: int | str) -> str:
    return str(cik).zfill(10)


def fetch_company_facts(cik: int | str, *,
                        force: bool = False) -> Path | None:
    """단일 CIK 의 companyfacts.json 다운로드 → 캐시 파일 경로 반환.

    404 (CIK 가 EDGAR 에 facts 없는 경우 — ADR 의 일부 항목) → None.
    """
    cik10 = _cik10(cik)
    dest = _raw_root() / f"CIK{cik10}.json"
    ckpt = CheckpointStore(_SOURCE)
    key = f"facts|{cik10}"

    if not force and dest.exists() and ckpt.is_done(key):
        log.info("[sec_oem] skip CIK%s — 캐시 hit", cik10)
        ckpt.mark_skipped()
        return dest

    _LIMITER.acquire()
    with SecEdgarClient(user_agent=_USER_AGENT) as client:
        try:
            data = client.get_company_facts(cik10)
        except Exception as e:   # noqa: BLE001
            log.exception("[sec_oem] CIK%s facts fetch failed", cik10)
            ckpt.mark_failed(key, str(e))
            return None
        if data is None:
            log.warning("[sec_oem] CIK%s — 404 (XBRL facts 없음)", cik10)
            ckpt.mark_failed(key, "404")
            return None

    save_raw(_SOURCE, f"CIK{cik10}.json", data)
    log.info("[sec_oem] CIK%s saved (%d concepts)", cik10,
             len(((data.get("facts") or {}).get("us-gaap") or {})))
    ckpt.mark_done(key, {"cik": cik10})
    return dest


def fetch_submissions(cik: int | str, *,
                      force: bool = False) -> Path | None:
    """단일 CIK 의 submissions.json — 최근 1000건 filings 메타. 본 모듈에서는 옵션."""
    cik10 = _cik10(cik)
    sub_dir = _raw_root() / "submissions"
    sub_dir.mkdir(exist_ok=True)
    dest = sub_dir / f"CIK{cik10}.json"
    ckpt = CheckpointStore(_SOURCE)
    key = f"submissions|{cik10}"

    if not force and dest.exists() and ckpt.is_done(key):
        ckpt.mark_skipped()
        return dest

    _LIMITER.acquire()
    with SecEdgarClient(user_agent=_USER_AGENT) as client:
        try:
            data = client.get_submissions(cik10)
        except Exception as e:   # noqa: BLE001
            log.exception("[sec_oem] CIK%s submissions failed", cik10)
            ckpt.mark_failed(key, str(e))
            return None
        if data is None:
            ckpt.mark_failed(key, "404")
            return None

    save_raw(_SOURCE, f"submissions/CIK{cik10}.json", data)
    ckpt.mark_done(key, {"cik": cik10})
    return dest


def ingest_all(*, ciks: list[int] | None = None,
               include_submissions: bool = False,
               force: bool = False) -> dict[str, int]:
    """OEM 시드 또는 명시된 CIK 전체. dict 통계 반환."""
    target_ciks = ciks if ciks else [c for c, *_ in OEM_SEED]
    stats = {"fetched": 0, "missing": 0, "skipped": 0,
             "submissions_fetched": 0}
    for cik in target_ciks:
        p = fetch_company_facts(cik, force=force)
        if p is None:
            stats["missing"] += 1
        else:
            stats["fetched"] += 1
        if include_submissions:
            sp = fetch_submissions(cik, force=force)
            if sp is not None:
                stats["submissions_fetched"] += 1
    log.info("[sec_oem] done: %s", stats)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.sec_oem")
    ap.add_argument("--cik", type=int, default=None,
                    help="단일 CIK (지정 안 하면 OEM_SEED 전체)")
    ap.add_argument("--ciks", help="콤마 구분 (예: 1318605,37996)")
    ap.add_argument("--include-submissions", action="store_true",
                    help="submissions.json 도 함께 (10-K 본문 URL 가져올 때만)")
    ap.add_argument("--no-cache", action="store_true",
                    help="캐시 무시 — 재다운로드 (XBRL 갱신 반영)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    ciks: list[int] | None = None
    if args.cik is not None:
        ciks = [args.cik]
    elif args.ciks:
        ciks = [int(c) for c in args.ciks.split(",") if c.strip()]

    ingest_all(ciks=ciks,
               include_submissions=args.include_submissions,
               force=args.no_cache)


if __name__ == "__main__":
    main()


__all__ = [
    "fetch_company_facts",
    "fetch_submissions",
    "ingest_all",
    "OEM_SEED",
]
