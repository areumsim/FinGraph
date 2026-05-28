"""data.go.kr 15089863 — 자동차 리콜정보 (Open API) 수집.

NHTSA Recalls (글로벌) 보완용 한국 KOTSA (한국교통안전공단) 리콜 데이터.

API: https://www.data.go.kr/data/15089863/openapi.do
응답: JSON (page-based, perPage 기본 100).

키 미설정 시 graceful skip — exit 0 + 로그.

저장:
    data/raw/auto/datagokr_recalls/page_{N}.json
    data/raw/auto/datagokr_recalls/_checkpoint.json

CLI:
    python -m autograph.ingestion.datagokr_recalls
    python -m autograph.ingestion.datagokr_recalls --start-page 1 --max-pages 50
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
import urllib.request

from autonexusgraph.ingestion._common import (
    CheckpointStore,
    RateLimiter,
    save_raw,
)

from ..config import get_auto_settings


log = logging.getLogger(__name__)


_SOURCE = "auto/datagokr_recalls"
_LIMITER = RateLimiter(per_sec=2.0)


def _fetch_page(page: int, per_page: int = 100) -> dict:
    s = get_auto_settings()
    if not s.data_go_kr_api_key:
        return {}
    url = f"{s.data_go_kr_base_url}/15089863/v1/uddi:autorecall"
    params = {
        "serviceKey": s.data_go_kr_api_key,
        "page": page,
        "perPage": per_page,
        "returnType": "JSON",
    }
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    req = urllib.request.Request(f"{url}?{qs}", headers={
        "Accept": "application/json",
        "User-Agent": "AutoGraph-Research/0.1",
    })
    _LIMITER.wait()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.error("[datagokr_recalls] page=%d HTTP %s: %s", page, e.code, e.reason)
        return {}
    except Exception as e:  # noqa: BLE001
        log.error("[datagokr_recalls] page=%d 실패: %s", page, e)
        return {}


def run(*, start_page: int = 1, max_pages: int = 50, per_page: int = 100) -> int:
    """수집 본체. 키 미설정 시 0 반환 (graceful skip)."""
    s = get_auto_settings()
    if not s.data_go_kr_api_key:
        log.warning("[datagokr_recalls] DATA_GO_KR_API_KEY 미설정 — graceful skip")
        return 0

    ckpt = CheckpointStore(_SOURCE)
    total_rows = 0

    for page in range(start_page, start_page + max_pages):
        key = f"page:{page}"
        if ckpt.is_done(key):
            continue
        payload = _fetch_page(page, per_page=per_page)
        if not payload:
            log.warning("[datagokr_recalls] page=%d 빈 응답 — 종료", page)
            break
        items = payload.get("data") or payload.get("items") or []
        if not items:
            log.info("[datagokr_recalls] page=%d 데이터 없음 — 종료", page)
            break
        save_raw(_SOURCE, f"page_{page:04d}.json", payload)
        ckpt.mark_done(key)
        total_rows += len(items)
        log.info("[datagokr_recalls] page=%d rows=%d (cumulative=%d)",
                 page, len(items), total_rows)
        if len(items) < per_page:
            break

    log.info("[datagokr_recalls] 완료. 누적 rows=%d", total_rows)
    return total_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--per-page", type=int, default=100)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    n = run(start_page=args.start_page, max_pages=args.max_pages, per_page=args.per_page)
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run"]
