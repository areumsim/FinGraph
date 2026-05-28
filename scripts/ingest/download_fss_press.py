#!/usr/bin/env python3
"""FSS 금융감독원 보도자료 수집.

라이선스: 공공누리 1유형 — 본문 저장·재배포 OK (출처 표기 필요).

전략:
- 일자별 페이지네이션 → 게시글 목록 수집
- 매 항목별 본문 fetch (HTML)
- raw/fss/press/<YYYY>/<article_id>/{meta.json, body.html}

사용:
    python scripts/ingest/download_fss_press.py [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                                                  [--pages 5] [--limit-bodies 50]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.ingestion._common import (
    CheckpointStore, fetch_with_retry, get_rate_limiter, save_raw,
)
from autonexusgraph.ingestion.fss_client import FssClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--to",   dest="to_date",   default=None, help="YYYY-MM-DD")
    parser.add_argument("--pages", type=int, default=5, help="목록 페이지 N개")
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--limit-bodies", type=int, default=200,
                        help="본문까지 다운로드할 최대 게시글 수")
    parser.add_argument("--no-body", action="store_true", help="목록만, 본문 skip")
    args = parser.parse_args()

    # 기본: 최근 365일
    today = date.today()
    if not args.to_date:
        args.to_date = today.isoformat()
    if not args.from_date:
        args.from_date = (today - timedelta(days=365)).isoformat()

    limiter = get_rate_limiter("fss_press")
    ckpt_list = CheckpointStore("fss_press_list")
    ckpt_body = CheckpointStore("fss_press_body")

    all_articles: list[dict] = []
    print(f"[fss_press] from={args.from_date} to={args.to_date} pages={args.pages}")

    with FssClient() as cli:
        # 목록 페이지 수집
        for page in range(1, args.pages + 1):
            limiter.acquire()
            try:
                arts = fetch_with_retry(
                    lambda p=page: cli.list_press_releases(
                        page=p, size=args.page_size,
                        date_from=args.from_date, date_to=args.to_date,
                    ),
                    max_tries=3,
                )
            except Exception as e:
                print(f"  [page {page}] list failed: {e}", file=sys.stderr)
                ckpt_list.mark_failed(f"page_{page}", str(e))
                continue

            print(f"  [page {page}] {len(arts)} articles")
            for a in arts:
                meta = {
                    "article_id": a.article_id,
                    "title": a.title,
                    "published_at": a.published_at,
                    "category": a.category,
                    "source_url": a.source_url,
                }
                all_articles.append(meta)
                # 메타만 raw 에 저장 (overwrite OK — 멱등)
                save_raw("fss_press",
                         f"{a.published_at[:4] if a.published_at else 'unknown'}/{a.article_id}/meta.json",
                         meta)
            ckpt_list.mark_done(f"page_{page}", {"count": len(arts)})

        print(f"\n[fss_press] total list: {len(all_articles)}")

        if args.no_body:
            return 0

        # 본문 fetch (limit-bodies 만큼)
        body_targets = [a for a in all_articles if not ckpt_body.is_done(a["article_id"])]
        body_targets = body_targets[: args.limit_bodies]
        print(f"[fss_press] fetching bodies: {len(body_targets)}")

        for i, meta in enumerate(body_targets, 1):
            limiter.acquire()
            try:
                body_data = fetch_with_retry(
                    lambda m=meta: cli.fetch_article_body(m["article_id"]),
                    max_tries=3,
                )
                year = (meta.get("published_at") or "")[:4] or "unknown"
                rel = f"{year}/{meta['article_id']}"
                if body_data.get("body_html"):
                    save_raw("fss_press", f"{rel}/body.html", body_data["body_html"])
                save_raw("fss_press", f"{rel}/body_meta.json", {
                    "article_id": meta["article_id"],
                    "attachment_urls": body_data.get("attachment_urls", []),
                    "fetched_at": time.time(),
                })
                ckpt_body.mark_done(meta["article_id"])
                if i % 20 == 0:
                    print(f"  [{i}/{len(body_targets)}] done={ckpt_body.stats.done}")
            except Exception as e:
                ckpt_body.mark_failed(meta["article_id"], str(e))

    print(f"\n[fss_press] done={ckpt_body.stats.done} failed={ckpt_body.stats.failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
