"""NHTSA / vPIC 인제스션 공통 helper.

세 ingestion 모듈 (nhtsa_vpic, nhtsa_recalls, nhtsa_complaints) 가 거의 동일한
``_http_get`` 과 ``_models_from_vpic`` 를 복제하던 것을 통합. 호출 모듈은 본인의
``RateLimiter`` 인스턴스를 만들어 ``nhtsa_http_get`` 에 전달 — 모듈별 rate-limit 독립.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from autonexusgraph.config import get_settings
from autonexusgraph.ingestion._common import RateLimiter, fetch_with_retry
from ..config import get_auto_settings


def nhtsa_http_get(url: str,
                   params: dict | None,
                   limiter: RateLimiter,
                   *, timeout: float = 30.0,
                   max_tries: int = 5) -> dict[str, Any]:
    """NHTSA / vPIC JSON GET with rate-limit + exponential backoff retry.

    호출자가 본인의 ``RateLimiter`` 를 주입해 모듈별 (vpic 5 req/sec, recalls/complaints
    4 req/sec) 독립 제어. 헤더는 AutoSettings.wikidata_user_agent (UA 공통).

    중요 — NHTSA Recalls/Complaints API 400 의 특이 동작:
        HTTP 400 임에도 body 는 `{"Count":0,"Message":"Results returned successfully",
        "results":[]}` 인 경우가 매우 흔하다 (해당 make/model/year 에 결과 없음의 의미).
        이를 에러로 처리하면 정상 "결과 없음" 케이스가 56% 까지 실패로 카운트됨 (실측).
        → 400 응답이지만 JSON body 가 정상 형식이면 그대로 반환.
    """
    settings = get_auto_settings()
    headers = {"User-Agent": settings.wikidata_user_agent}

    def _do() -> dict[str, Any]:
        with httpx.Client(timeout=timeout, headers=headers) as client:
            r = client.get(url, params=params)
            if r.status_code == 400:
                # NHTSA "결과 없음" 으로 400 + JSON body 보내는 케이스 흡수.
                try:
                    body = r.json()
                except Exception:   # noqa: BLE001
                    body = None
                if isinstance(body, dict) and (
                    "Count" in body or "results" in body or "Results" in body
                ):
                    return body
            r.raise_for_status()
            return r.json()

    limiter.acquire()
    return fetch_with_retry(_do, max_tries=max_tries)


def models_from_vpic(make: str, year: int) -> list[str]:
    """vpic raw 캐시 (``data/raw/auto/nhtsa_vpic/{MAKE}/{YEAR}/variants.jsonl``) 에서
    모델명 목록을 읽음. 캐시 없으면 빈 리스트 (호출자가 warning 처리)."""
    fg = get_settings()
    p = fg.ingest_raw_dir / "auto" / "nhtsa_vpic" / make / str(year) / "variants.jsonl"
    if not p.exists():
        return []
    out: list[str] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                m = row.get("model_name")
                if m:
                    out.append(m)
            except json.JSONDecodeError:
                continue
    return sorted(set(out))


__all__ = ["nhtsa_http_get", "models_from_vpic"]
