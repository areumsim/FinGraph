"""KATRI (한국자동차연구원) / bigdata-tic.kr — 시험인증·부품 인증 데이터 수집.

OAuth 2 client_credentials grant 로 토큰 발급 → API 호출.

키 (`.env`):
    BIGDATA_TIC_CLIENT_ID
    BIGDATA_TIC_CLIENT_SECRET
    BIGDATA_TIC_BASE_URL (default: https://oauth.bigdata-tic.kr)

키 미설정 또는 토큰 발급 실패 시 graceful skip — exit 0.

CLI:
    python -m autograph.ingestion.katri_tic
    python -m autograph.ingestion.katri_tic --probe   # 토큰 발급 확인만
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
import urllib.request
from typing import Any

from autonexusgraph.ingestion._common import RateLimiter, save_raw

from ..config import get_auto_settings


log = logging.getLogger(__name__)


_SOURCE = "auto/katri_tic"
_LIMITER = RateLimiter(per_sec=2.0)


def _fetch_token() -> str | None:
    s = get_auto_settings()
    if not (s.bigdata_tic_client_id and s.bigdata_tic_client_secret):
        return None
    url = f"{s.bigdata_tic_base_url}/oauth2/token"
    data = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     s.bigdata_tic_client_id,
        "client_secret": s.bigdata_tic_client_secret,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept":       "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("access_token")
    except Exception as exc:  # noqa: BLE001
        log.warning("[katri] token 발급 실패: %s", exc)
        return None


def _fetch_endpoint(token: str, endpoint: str, params: dict) -> dict[str, Any]:
    s = get_auto_settings()
    url = f"{s.bigdata_tic_base_url}{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json",
    })
    _LIMITER.wait()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.error("[katri] %s 실패: %s", endpoint, exc)
        return {}


# KATRI 시험인증 데이터 (예시 — 실제 endpoint 는 API 발급 후 명세에 따라 교체).
_KATRI_ENDPOINTS: list[tuple[str, dict]] = [
    ("/api/v1/cert/parts",         {}),
    ("/api/v1/cert/safety_tests",  {}),
]


def run(*, probe: bool = False) -> int:
    token = _fetch_token()
    if not token:
        log.warning(
            "[katri] BIGDATA_TIC_CLIENT_ID/SECRET 미설정 또는 토큰 발급 실패 — graceful skip",
        )
        return 0

    log.info("[katri] 토큰 발급 성공 — 길이=%d", len(token))
    if probe:
        return 1

    total = 0
    for endpoint, params in _KATRI_ENDPOINTS:
        payload = _fetch_endpoint(token, endpoint, params)
        if not payload:
            continue
        safe_name = endpoint.replace("/", "_").strip("_") + ".json"
        save_raw(_SOURCE, safe_name, payload)
        items = payload.get("data") or payload.get("items") or []
        log.info("[katri] %s rows=%d", endpoint, len(items))
        total += len(items)

    log.info("[katri] 완료. 누적 rows=%d", total)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="토큰 발급만 확인")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(probe=args.probe)
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run"]
