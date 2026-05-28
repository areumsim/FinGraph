"""NHTSA vPIC API — 제조사·모델·연식·제원 수집.

엔드포인트 (키 불필요):
- /vehicles/GetAllMakes?format=json
- /vehicles/GetModelsForMakeYear/make/{make}/modelyear/{year}?format=json
- /vehicles/GetCanadianVehicleSpecifications/?Year={year}&Make={make}&Model={model}&units= ...

저장:
    data/raw/auto/nhtsa_vpic/all_makes.json
    data/raw/auto/nhtsa_vpic/{make}/{year}/models.json
    data/raw/auto/nhtsa_vpic/{make}/{year}/variants.jsonl

CLI:
    python -m autograph.ingestion.nhtsa_vpic --make HYUNDAI --year 2024
    python -m autograph.ingestion.nhtsa_vpic --makes HYUNDAI,KIA --years 2022-2024
    python -m autograph.ingestion.nhtsa_vpic --all-makes        # 전체 제조사 마스터만
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from fingraph.ingestion._common import (
    CheckpointStore,
    RateLimiter,
    save_raw,
)
from ..config import get_auto_settings
from ._common_nhtsa import nhtsa_http_get


log = logging.getLogger(__name__)

# vPIC 은 무료라도 폭주 방지 — 5 req/sec 보수적.
_LIMITER = RateLimiter(per_sec=5.0)
_SOURCE = "auto/nhtsa_vpic"


def _http_get(url: str, params: dict | None = None) -> dict:
    return nhtsa_http_get(url, params, _LIMITER)


# ─── all makes ─────────────────────────────────────────────────
def fetch_all_makes() -> dict:
    """전체 제조사 마스터 (vPIC MakeId + MakeName)."""
    settings = get_auto_settings()
    url = f"{settings.nhtsa_vpic_base_url}/vehicles/GetAllMakes"
    data = _http_get(url, {"format": "json"})
    save_raw(_SOURCE, "all_makes.json", data)
    log.info("[vpic] all_makes count=%s", data.get("Count"))
    return data


# ─── models for (make, year) ───────────────────────────────────
def fetch_models_for_make_year(make: str, year: int) -> list[dict]:
    """제조사 × 연식 → 모델 목록."""
    settings = get_auto_settings()
    url = (
        f"{settings.nhtsa_vpic_base_url}/vehicles/GetModelsForMakeYear/"
        f"make/{make}/modelyear/{year}"
    )
    data = _http_get(url, {"format": "json"})
    rel = f"{make}/{year}/models.json"
    save_raw(_SOURCE, rel, data)
    rows: list[dict] = data.get("Results") or []
    log.info("[vpic] %s %s models=%d", make, year, len(rows))
    return rows


# ─── variants — Canadian spec (단순 dim/weight) ────────────────
def fetch_canadian_specs(make: str, year: int, model: str | None = None) -> dict:
    """Canadian Vehicle Specifications (제원 일부). model 생략 시 make+year 전체."""
    settings = get_auto_settings()
    params: dict[str, Any] = {"Year": year, "Make": make, "format": "json"}
    if model:
        params["Model"] = model
    url = f"{settings.nhtsa_vpic_base_url}/vehicles/GetCanadianVehicleSpecifications/"
    data = _http_get(url, params)
    rel = f"{make}/{year}/canspec_{model or 'ALL'}.json"
    save_raw(_SOURCE, rel, data)
    return data


# ─── 통합 fetch ────────────────────────────────────────────────
def ingest_make_year(make: str, year: int, *, with_canspec: bool = True) -> dict:
    """make × year 한 조합. checkpoint resume 안전."""
    ckpt = CheckpointStore(_SOURCE)
    key = f"{make}|{year}"
    if ckpt.is_done(key):
        ckpt.mark_skipped()
        log.info("[vpic] skip %s (already done)", key)
        return {"skipped": True}

    try:
        models = fetch_models_for_make_year(make, year)
        variants_path = save_raw(_SOURCE, f"{make}/{year}/variants.jsonl", "")
        # variants.jsonl 은 모델별 한 줄 (variants는 적재 단계에서 model_year+trim 으로 펼친다)
        with variants_path.open("w", encoding="utf-8") as f:
            for m in models:
                row = {
                    "make": make,
                    "model_year": year,
                    "model_name": m.get("Model_Name"),
                    "make_id": m.get("Make_ID"),
                    "model_id_vpic": m.get("Model_ID"),
                    "raw": m,
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if with_canspec:
            try:
                fetch_canadian_specs(make, year)
            except httpx.HTTPError as e:
                log.warning("[vpic] canspec %s %s failed: %s", make, year, e)
        ckpt.mark_done(key, {"models": len(models)})
        return {"models": len(models)}
    except Exception as e:  # noqa: BLE001
        log.exception("[vpic] failed %s", key)
        ckpt.mark_failed(key, str(e))
        return {"error": str(e)}


# ─── CLI ───────────────────────────────────────────────────────
def _parse_years(spec: str) -> list[int]:
    """'2022-2024' or '2024' or '2020,2022,2024'."""
    spec = spec.strip()
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    if "," in spec:
        return [int(x) for x in spec.split(",") if x.strip()]
    return [int(spec)]


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.nhtsa_vpic")
    ap.add_argument("--make", help="단일 제조사 (예: HYUNDAI)")
    ap.add_argument("--makes", help="콤마 구분 (예: HYUNDAI,KIA)")
    ap.add_argument("--year", type=int, help="단일 연식")
    ap.add_argument("--years", help="범위 또는 콤마 (예: 2022-2024)")
    ap.add_argument("--all-makes", action="store_true",
                    help="전체 제조사 마스터만 받기 (모델/연식 skip)")
    ap.add_argument("--no-canspec", action="store_true", help="Canadian specs skip")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.all_makes:
        fetch_all_makes()
        return

    makes: list[str] = []
    if args.make:
        makes.append(args.make.strip().upper())
    if args.makes:
        makes.extend([m.strip().upper() for m in args.makes.split(",") if m.strip()])
    if not makes:
        makes = [m.strip().upper() for m in
                 get_auto_settings().auto_ingest_makes.split(",") if m.strip()]

    years: list[int] = []
    if args.year is not None:
        years.append(args.year)
    if args.years:
        years.extend(_parse_years(args.years))
    if not years:
        s = get_auto_settings()
        years = list(range(s.auto_ingest_year_min, s.auto_ingest_year_max + 1))

    # 매번 호출 전에 all_makes 도 한 번 보장 (있으면 캐시).
    fetch_all_makes()

    for make in makes:
        for year in years:
            ingest_make_year(make, year, with_canspec=not args.no_canspec)


if __name__ == "__main__":
    main()
