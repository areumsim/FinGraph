#!/usr/bin/env python3
"""Wikipedia 한국어 페이지 다운로드 — corp_code 별 summary + html + infobox.

대상 선정:
1. Wikidata 가 매칭된 corp (sitelinks.kowiki.title 사용 — 가장 정확)
2. 그 외는 corp_name 으로 직접 시도 (search fallback)

저장:
  data/raw/wikipedia/ko/<corp_code>/summary.json
  data/raw/wikipedia/ko/<corp_code>/page.html
  data/raw/wikipedia/ko/<corp_code>/infobox.json
  data/raw/wikipedia/ko/<corp_code>/meta.json   (title, page_id, revision_id, fetched_at)

사용:
    python scripts/ingest/download_wikipedia.py [--lang ko|en] [--limit N] [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_pool
from autonexusgraph.ingestion._common import (
    CheckpointStore, fetch_with_retry, get_rate_limiter, save_raw,
)
from autonexusgraph.ingestion.wikipedia_client import WikipediaClient


def _select_targets(lang: str) -> list[dict]:
    """매핑된 회사 + 매핑 안된 회사 모두 → fallback 으로 시도.

    return: [{corp_code, corp_name, wiki_title or None}]
    """
    s = get_settings()
    matched_path = s.ingest_raw_dir / "wikidata" / "matched.jsonl"
    qid_to_title: dict[str, str] = {}

    # Wikidata entity 의 sitelinks 에서 kowiki.title 추출
    entities_dir = s.ingest_raw_dir / "wikidata" / "entities"
    if matched_path.exists() and entities_dir.exists():
        for line in matched_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            m = json.loads(line)
            qid = m["qid"]
            ep = entities_dir / f"{qid}.json"
            if not ep.exists():
                continue
            try:
                entity = json.loads(ep.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            site_key = f"{lang}wiki"
            sl = entity.get("sitelinks", {}).get(site_key, {})
            title = sl.get("title")
            if title:
                qid_to_title[qid] = title

    # PG 에서 corp_code 목록 + wikidata_qid 매핑
    pool = get_pool()
    targets: list[dict] = []
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT c.corp_code, c.corp_name,
                   (SELECT id_value FROM master.entity_map em
                     WHERE em.corp_code = c.corp_code AND em.id_type='wikidata_qid'
                     LIMIT 1) as qid
              FROM master.companies c
             WHERE c.is_active = TRUE
             ORDER BY c.corp_code
        """)
        for corp_code, corp_name, qid in cur.fetchall():
            targets.append({
                "corp_code": corp_code,
                "corp_name": corp_name,
                "wiki_title": qid_to_title.get(qid) if qid else None,
            })
    return targets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", choices=["ko", "en"], default="ko")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    targets = _select_targets(args.lang)
    if args.limit:
        targets = targets[: args.limit]

    print(f"[wikipedia] lang={args.lang} targets={len(targets)}")
    print(f"[wikipedia]   with_wikidata_title: {sum(1 for t in targets if t['wiki_title'])}")
    print(f"[wikipedia]   without_title (fallback): {sum(1 for t in targets if not t['wiki_title'])}")

    ckpt = CheckpointStore(f"wikipedia_{args.lang}")
    limiter = get_rate_limiter("wikipedia")

    with WikipediaClient(lang=args.lang) as wp:
        for i, t in enumerate(targets, 1):
            corp_code = t["corp_code"]
            if ckpt.is_done(corp_code) and not args.force:
                continue
            title = t["wiki_title"] or t["corp_name"]

            limiter.acquire()
            try:
                page = fetch_with_retry(
                    lambda: wp.fetch(title, with_html=True, with_infobox=True),
                    max_tries=3,
                )
                if page is None and not t["wiki_title"]:
                    # corp_name 그대로 실패 → search fallback
                    limiter.acquire()
                    hits = wp.search(t["corp_name"], limit=3)
                    for h in hits:
                        if "회사" in h.get("snippet", "") or "기업" in h.get("snippet", ""):
                            limiter.acquire()
                            page = fetch_with_retry(
                                lambda: wp.fetch(h["title"], with_html=True, with_infobox=True),
                                max_tries=3,
                            )
                            if page:
                                break

                if page is None:
                    ckpt.mark_failed(corp_code, "page_not_found")
                    continue

                # 저장 — html 은 별도 (큼)
                rel_root = f"{args.lang}/{corp_code}"
                save_raw("wikipedia", f"{rel_root}/summary.json", page.raw_summary or {})
                if page.html:
                    save_raw("wikipedia", f"{rel_root}/page.html", page.html)
                if page.infobox:
                    save_raw("wikipedia", f"{rel_root}/infobox.json", page.infobox)
                save_raw("wikipedia", f"{rel_root}/meta.json", {
                    "corp_code": corp_code,
                    "title": page.title,
                    "page_id": page.page_id,
                    "revision_id": page.revision_id,
                    "last_modified": page.last_modified,
                    "extract": page.extract,
                    "fetched_at": time.time(),
                })
                ckpt.mark_done(corp_code, {"title": page.title})
                if i % 20 == 0:
                    print(f"  [{i}/{len(targets)}] done={ckpt.stats.done} "
                          f"failed={ckpt.stats.failed}")
            except Exception as e:
                ckpt.mark_failed(corp_code, str(e))

    print(f"\n[wikipedia] done={ckpt.stats.done} failed={ckpt.stats.failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
