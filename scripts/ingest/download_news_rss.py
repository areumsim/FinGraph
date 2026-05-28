#!/usr/bin/env python3
"""한국어 뉴스 RSS 점진 수집 — 연합뉴스 + 정부 RSS.

라이선스:
- 연합뉴스: 본문 저장 금지 → 제목+요약+URL+메타만
- 정부 RSS (mois/moef): 공공누리 → 본문 저장 OK
- _common.save_raw 가 LICENSE_POLICY 기반으로 자동 처리

저장:
    data/raw/news/<feed>/<YYYYMMDD>/<guid_hash>.json

사용:
    python scripts/ingest/download_news_rss.py
    python scripts/ingest/download_news_rss.py --feeds yonhap_economy,mois_press
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.ingestion._common import (
    CheckpointStore, fetch_with_retry, get_rate_limiter, save_raw,
)
from autonexusgraph.ingestion._license import policy
from autonexusgraph.ingestion.news_client import KOREAN_FEEDS, NewsRssClient


def _article_hash(source: str, link: str) -> str:
    return hashlib.sha256(f"{source}||{link}".encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feeds", default=None,
                        help=f"쉼표 구분 (기본: 전체). 가능: {','.join(KOREAN_FEEDS)}")
    args = parser.parse_args()

    if args.feeds:
        wanted = {f.strip() for f in args.feeds.split(",")}
        feeds = {k: v for k, v in KOREAN_FEEDS.items() if k in wanted}
    else:
        feeds = dict(KOREAN_FEEDS)

    print(f"[news_rss] feeds={list(feeds)}")

    ckpt = CheckpointStore("news_rss")
    limiter = get_rate_limiter("news_rss")

    total = 0
    new_count = 0
    with NewsRssClient() as cli:
        for name, url in feeds.items():
            limiter.acquire()
            print(f"\n--- {name}: {url}")
            try:
                items = fetch_with_retry(lambda u=url, n=name: cli.fetch(u, source_name=n),
                                         max_tries=3)
            except Exception as e:
                print(f"   fetch failed: {e}")
                ckpt.mark_failed(name, str(e))
                continue

            print(f"   parsed {len(items)} items")
            total += len(items)

            # license tier 는 feed 이름 별로 다름 — news_yonhap / news_mois / news_moef 매핑
            license_key = (
                "news_yonhap" if name.startswith("yonhap") else
                "news_mois"   if name == "mois_press" else
                "news_moef"   if name == "moef_press" else
                "news_other"
            )

            for it in items:
                ah = _article_hash(name, it.link)
                if ckpt.is_done(ah):
                    continue
                payload = {
                    "article_hash": ah,
                    "source": name,
                    "license_key": license_key,
                    "license_tier": policy(license_key),
                    "guid": it.guid,
                    "title": it.title,
                    "summary": it.summary,
                    "link": it.link,
                    "published_at": it.published_at,
                    "categories": it.categories,
                }
                # save_raw 가 license 기반으로 body 필드 strip — 여기선 body 없음
                date_part = (it.published_at[:10] if it.published_at else
                             date.today().isoformat()).replace("-", "")
                save_raw("news", f"{name}/{date_part}/{ah}.json", payload)
                ckpt.mark_done(ah, {"source": name})
                new_count += 1

    print(f"\n[news_rss] total_parsed={total} new_saved={new_count} "
          f"(skipped existing={ckpt.stats.done - new_count})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
