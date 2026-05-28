"""NHTSA Complaints API — 결함 신고/소비자 불만 텍스트 수집.

엔드포인트:
    GET https://api.nhtsa.gov/complaints/complaintsByVehicle
        ?make=HYUNDAI&model=SONATA&modelYear=2024

응답 (대략):
    {
      "count": N,
      "results": [
        {
          "odiNumber": "11581234", "manufacturer": "...",
          "components": "ENGINE; ELECTRICAL SYSTEM",
          "summary": "...", "products": [...],
          "dateOfIncident": "2024-03-01", "dateComplaintFiled": "2024-03-15",
          "vin": "...", "modelYear": "2024", "make": "HYUNDAI", "model": "SONATA"
        }, ...
      ]
    }

저장: data/raw/auto/nhtsa_complaints/{MAKE}/{MODEL}/{YEAR}.json

CLI:
    python -m autograph.ingestion.nhtsa_complaints --make HYUNDAI --year 2024
"""

from __future__ import annotations

import argparse
import logging

from fingraph.ingestion._common import (
    CheckpointStore,
    RateLimiter,
    save_raw,
)
from ..config import get_auto_settings
from ._common_nhtsa import models_from_vpic as _models_from_vpic, nhtsa_http_get


log = logging.getLogger(__name__)

_LIMITER = RateLimiter(per_sec=4.0)
_SOURCE = "auto/nhtsa_complaints"


def _http_get(url: str, params: dict) -> dict:
    return nhtsa_http_get(url, params, _LIMITER)


def fetch_complaints(make: str, model: str, year: int) -> dict:
    settings = get_auto_settings()
    url = f"{settings.nhtsa_api_base_url}/complaints/complaintsByVehicle"
    data = _http_get(url, {"make": make, "model": model, "modelYear": year})
    rel = f"{make}/{model}/{year}.json"
    save_raw(_SOURCE, rel, data)
    n = len(data.get("results") or [])
    log.info("[complaints] %s %s %s -> %d", make, model, year, n)
    return data


def ingest_make_year(make: str, year: int, *,
                     models: list[str] | None = None) -> dict:
    if not models:
        models = _models_from_vpic(make, year)
    if not models:
        log.warning("[complaints] %s %s: models 비어있음 — vpic 먼저 또는 --models 지정", make, year)
        return {"models": 0}

    ckpt = CheckpointStore(_SOURCE)
    n_done = 0
    n_complaints = 0
    for model in models:
        key = f"{make}|{model}|{year}"
        if ckpt.is_done(key):
            ckpt.mark_skipped()
            continue
        try:
            data = fetch_complaints(make, model, year)
            n_done += 1
            n_complaints += len(data.get("results") or [])
            ckpt.mark_done(key, {"complaints": len(data.get("results") or [])})
        except Exception as e:  # noqa: BLE001
            log.exception("[complaints] failed %s", key)
            ckpt.mark_failed(key, str(e))

    return {"models_fetched": n_done, "complaints_total": n_complaints}


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.nhtsa_complaints")
    ap.add_argument("--make", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--models", help="콤마 구분. 없으면 vpic 캐시에서 가져옴.")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    models = None
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    out = ingest_make_year(args.make.upper(), args.year, models=models)
    log.info("[complaints] done %s", out)


if __name__ == "__main__":
    main()
