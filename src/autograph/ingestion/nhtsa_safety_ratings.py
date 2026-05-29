"""NHTSA SafetyRatings API — NCAP 5-star 점수·세부 등급 수집.

엔드포인트 (키 불필요):
    GET https://api.nhtsa.gov/SafetyRatings/modelyear/{YYYY}/make/{MAKE}/model/{MODEL}
        → {Count, Results: [{VehicleId, VehicleDescription, OverallRating, ...}]}

응답 핵심 필드:
    OverallRating               : "1"~"5" (전체)
    OverallFrontCrashRating     : 정면 전체
    FrontCrashDriversideRating  : 정면 운전석
    FrontCrashPassengersideRating
    OverallSideCrashRating      : 측면 전체
    SideCrashDriversideRating
    SideCrashPassengersideRating
    SidePoleCrashRating         : 측면 폴
    RolloverRating              : 전복 등급
    RolloverPossibility         : "12.34%" — 전복 확률
    NHTSAElectronicStabilityControl  : "Standard" | "Optional" | ...
    NHTSAForwardCollisionWarning
    NHTSALaneDepartureWarning

저장:
    data/raw/auto/nhtsa_safety/{MAKE}/{MODEL}/{YEAR}.json

models 목록은 ``data/raw/auto/nhtsa_vpic/{make}/{year}/variants.jsonl`` 에서 가져온다
(vpic 적재 선행 필요). 없으면 --models 인자로 직접 지정.

CLI:
    python -m autograph.ingestion.nhtsa_safety_ratings --make HYUNDAI --year 2024
    python -m autograph.ingestion.nhtsa_safety_ratings --make TESLA --year 2023 --models "MODEL Y"
"""

from __future__ import annotations

import argparse
import logging

from autonexusgraph.ingestion._common import (
    CheckpointStore,
    RateLimiter,
    save_raw,
)
from ..config import get_auto_settings
from ._common_nhtsa import models_from_vpic as _models_from_vpic, nhtsa_http_get


log = logging.getLogger(__name__)

# vPIC / Recalls / Complaints 와 동일한 보수적 4 req/sec.
_LIMITER = RateLimiter(per_sec=4.0)
_SOURCE = "auto/nhtsa_safety"


def _http_get(url: str, params: dict | None = None) -> dict:
    return nhtsa_http_get(url, params, _LIMITER)


def fetch_safety_ratings(make: str, model: str, year: int) -> dict:
    """단일 (make, model, year) 조합 NCAP 등급. **2단계**:
        1) /SafetyRatings/modelyear/.../make/.../model/...  → VehicleId 리스트만
        2) /SafetyRatings/VehicleId/{id}                    → 실제 등급 detail
    각 Results 항목에 detail 을 inline merge 한 뒤 raw 저장.
    """
    settings = get_auto_settings()
    base = settings.nhtsa_api_base_url
    list_url = f"{base}/SafetyRatings/modelyear/{year}/make/{make}/model/{model}"
    listing = _http_get(list_url)
    results = listing.get("Results") or []

    # 각 VehicleId 의 detail 호출 + merge.
    enriched: list[dict] = []
    for row in results:
        vid = row.get("VehicleId")
        merged = dict(row)
        if vid is not None:
            try:
                detail = _http_get(f"{base}/SafetyRatings/VehicleId/{vid}")
                # detail.Results 의 첫 row 가 실제 등급 dict.
                dres = (detail.get("Results") or [None])[0] or {}
                # listing 의 VehicleDescription/VehicleId 유지, detail 의 모든 등급 필드 덮어쓰기.
                for k, v in dres.items():
                    if k not in merged or merged[k] in (None, ""):
                        merged[k] = v
            except Exception as e:   # noqa: BLE001
                log.warning("[safety] VehicleId=%s detail 실패: %s", vid, e)
        enriched.append(merged)

    out = {
        "Count": listing.get("Count"),
        "Message": listing.get("Message"),
        "Results": enriched,
    }
    rel = f"{make}/{model}/{year}.json"
    save_raw(_SOURCE, rel, out)
    log.info("[safety] %s %s %s -> %d rated trims (with detail)",
             make, model, year, len(enriched))
    return out


def ingest_make_year(make: str, year: int, *,
                     models: list[str] | None = None) -> dict:
    """make × year — vPIC 모델 목록 전체 또는 명시된 일부."""
    if not models:
        models = _models_from_vpic(make, year)
    if not models:
        log.warning("[safety] %s %s: models 비어있음 — vpic 먼저 또는 --models 지정",
                    make, year)
        return {"models": 0}

    ckpt = CheckpointStore(_SOURCE)
    n_done = 0
    n_rated_trims = 0
    for model in models:
        key = f"{make}|{model}|{year}"
        if ckpt.is_done(key):
            ckpt.mark_skipped()
            continue
        try:
            data = fetch_safety_ratings(make, model, year)
            n_done += 1
            n_rated_trims += len(data.get("Results") or [])
            ckpt.mark_done(key, {"rated_trims": len(data.get("Results") or [])})
        except Exception as e:  # noqa: BLE001
            log.exception("[safety] failed %s", key)
            ckpt.mark_failed(key, str(e))

    return {"models_fetched": n_done, "rated_trims_total": n_rated_trims}


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.nhtsa_safety_ratings")
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
    log.info("[safety] done %s", out)


if __name__ == "__main__":
    main()


__all__ = ["fetch_safety_ratings", "ingest_make_year"]
