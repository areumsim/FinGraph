#!/usr/bin/env python3
"""뉴스 RSS raw → PG news.articles + 회사 멘션 + Neo4j NewsEvent.

룰 기반 멘션 추출:
- 회사명·alias 정규화 후 기사 제목/요약에서 substring 매칭
- 너무 짧은 이름(2자 이하) 은 제외 (오탐 多)

Neo4j:
- (:NewsEvent {article_hash})-[:MENTIONS {confidence}]->(:Company)
- 공동 언급 → (:Company)-[:CO_MENTIONED_WITH {count}]-(:Company)  (집계는 별도 step)

사용:
    python scripts/load/load_news.py [--dry-run] [--no-neo4j]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_pool


UPSERT_ARTICLE = """
INSERT INTO news.articles
  (article_hash, source, guid, title, summary, body_text, link,
   published_at, categories, license_tier, raw)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (article_hash) DO UPDATE
   SET title       = EXCLUDED.title,
       summary     = EXCLUDED.summary,
       body_text   = EXCLUDED.body_text,
       categories  = EXCLUDED.categories,
       license_tier = EXCLUDED.license_tier
"""

UPSERT_MENTION = """
INSERT INTO news.article_mentions
  (article_hash, corp_code, extracted_by, confidence)
VALUES (%s, %s, %s, %s)
ON CONFLICT (article_hash, corp_code) DO UPDATE
   SET confidence = GREATEST(news.article_mentions.confidence, EXCLUDED.confidence)
"""

NEO4J_UPSERT_NEWS = """
UNWIND $rows AS r
MERGE (n:NewsEvent {article_hash: r.article_hash})
SET n.title       = r.title,
    n.source      = r.source,
    n.published_at = r.published_at,
    n.url         = r.link
WITH n, r
UNWIND r.corp_codes AS cc
MATCH (c:Company {corp_code: cc})
MERGE (n)-[m:MENTIONS]->(c)
SET m.extracted_by = 'rule',
    m.confidence = 0.8
"""


def _parse_published(s: str | None) -> datetime | None:
    """RSS pubDate / ISO8601 → datetime."""
    if not s:
        return None
    s = s.strip()
    # RFC822: 'Mon, 26 May 2025 13:24:00 +0900'
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _load_aliases() -> dict[str, set[str]]:
    """alias_norm → set(corp_code). 짧은 alias(≤2자) 제외."""
    pool = get_pool()
    out: dict[str, set[str]] = defaultdict(set)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT alias, alias_norm, corp_code
              FROM master.company_aliases
        """)
        for alias, alias_norm, corp_code in cur.fetchall():
            for k in (alias, alias_norm):
                if k and len(k) >= 3:
                    out[k].add(corp_code)
        # 회사명 자체도 추가
        cur.execute("SELECT corp_code, corp_name FROM master.companies WHERE is_active=TRUE")
        from autonexusgraph.ingestion._common import normalize_corp_name
        for corp_code, corp_name in cur.fetchall():
            if corp_name and len(corp_name) >= 3:
                out[corp_name].add(corp_code)
                out[normalize_corp_name(corp_name)].add(corp_code)
    return out


def _detect_mentions(text: str, aliases: dict[str, set[str]]) -> set[str]:
    """text 안에서 alias 매칭 → 회사 corp_code 집합."""
    if not text:
        return set()
    hits: set[str] = set()
    for alias, corp_codes in aliases.items():
        if alias in text:
            hits.update(corp_codes)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-neo4j", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    s = get_settings()
    news_root = s.ingest_raw_dir / "news"
    if not news_root.exists():
        print(f"{news_root} 없음", file=sys.stderr)
        return 2

    print("[load_news] loading aliases …")
    aliases = _load_aliases()
    print(f"[load_news] aliases loaded: {len(aliases)}")

    article_rows: list[tuple] = []
    mention_rows: list[tuple] = []
    neo4j_rows: list[dict] = []

    files = list(news_root.rglob("*.json"))
    if args.limit:
        files = files[:args.limit]
    print(f"[load_news] raw files: {len(files)}")

    for fp in files:
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        ah = d.get("article_hash")
        if not ah:
            continue

        pub = _parse_published(d.get("published_at"))
        text_for_match = f"{d.get('title','')} {d.get('summary','')}"
        hits = _detect_mentions(text_for_match, aliases)

        article_rows.append((
            ah,
            d.get("source", ""),
            d.get("guid"),
            (d.get("title") or "")[:500],
            d.get("summary"),
            d.get("body_text"),
            (d.get("link") or "")[:1000],
            pub,
            d.get("categories") or [],
            d.get("license_tier"),
            json.dumps(d, ensure_ascii=False),
        ))
        for cc in hits:
            mention_rows.append((ah, cc, "rule", 0.80))

        if hits:
            neo4j_rows.append({
                "article_hash": ah,
                "title": d.get("title", "")[:500],
                "source": d.get("source", ""),
                "published_at": pub.isoformat() if pub else None,
                "link": d.get("link", ""),
                "corp_codes": list(hits),
            })

    print(f"[load_news] articles={len(article_rows)} "
          f"mentions={len(mention_rows)} neo4j_events={len(neo4j_rows)}")

    if args.dry_run:
        for a in article_rows[:2]:
            print("  A:", a[:5])
        for m in mention_rows[:5]:
            print("  M:", m)
        return 0

    pool = get_pool()
    BATCH = 500
    with pool.connection() as conn, conn.cursor() as cur:
        for i in range(0, len(article_rows), BATCH):
            cur.executemany(UPSERT_ARTICLE, article_rows[i:i + BATCH])
        for i in range(0, len(mention_rows), BATCH):
            cur.executemany(UPSERT_MENTION, mention_rows[i:i + BATCH])

    if not args.no_neo4j and neo4j_rows:
        from autonexusgraph.db.neo4j import get_driver
        with get_driver().session() as session:
            for i in range(0, len(neo4j_rows), 100):
                session.run(NEO4J_UPSERT_NEWS, rows=neo4j_rows[i:i + 100])

    # 검증
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT source, count(*) FROM news.articles GROUP BY source ORDER BY 2 DESC")
        print("\n[news.articles by source]")
        for r in cur.fetchall():
            print(f"  {r[0]:25s} {r[1]:>6}")
        cur.execute("SELECT count(*) FROM news.article_mentions")
        print(f"[mentions] total: {cur.fetchone()[0]:,}")
        cur.execute("""
            SELECT corp_code, count(*) FROM news.article_mentions
            GROUP BY corp_code ORDER BY 2 DESC LIMIT 10
        """)
        print("[mentions] top corps:")
        for r in cur.fetchall():
            print(f"  {r[0]} : {r[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
