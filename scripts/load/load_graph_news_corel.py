#!/usr/bin/env python3
"""CO_MENTIONED_WITH — 뉴스에서 같은 기사에 함께 언급된 회사 쌍 집계.

(A, B) 와 (B, A) 는 같은 관계 — directed=false. 알파벳/사전순 정렬 후 보관.

산출: (:Company)-[:CO_MENTIONED_WITH {count, last_seen, sources}]-(:Company)
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.db.postgres import get_pool


CYPHER = """
UNWIND $rows AS r
MATCH (a:Company {corp_code: r.a})
MATCH (b:Company {corp_code: r.b})
MERGE (a)-[rel:CO_MENTIONED_WITH]-(b)
ON CREATE SET rel.count = r.count, rel.last_seen = r.last_seen, rel.sources = r.sources
ON MATCH  SET rel.count = r.count, rel.last_seen = r.last_seen, rel.sources = r.sources
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-count", type=int, default=2,
                        help="이 횟수 미만 쌍은 노이즈로 제외")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pool = get_pool()
    # article_hash → set(corp_code), with metadata
    article_to_corps: dict[str, set[str]] = defaultdict(set)
    article_meta: dict[str, dict] = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT m.article_hash, m.corp_code,
                   a.source, a.published_at::text
              FROM news.article_mentions m
              JOIN news.articles a ON a.article_hash = m.article_hash
        """)
        for ah, cc, source, pub in cur.fetchall():
            article_to_corps[ah].add(cc)
            article_meta[ah] = {"source": source, "published_at": pub}

    print(f"[news_corel] articles_with_mentions: {len(article_to_corps)}")

    pair_count: Counter[tuple[str, str]] = Counter()
    pair_last: dict[tuple[str, str], str] = {}
    pair_sources: dict[tuple[str, str], set[str]] = defaultdict(set)

    for ah, corps in article_to_corps.items():
        if len(corps) < 2:
            continue
        ccs = sorted(corps)
        meta = article_meta.get(ah, {})
        for i in range(len(ccs)):
            for j in range(i + 1, len(ccs)):
                k = (ccs[i], ccs[j])
                pair_count[k] += 1
                if meta.get("published_at"):
                    prev = pair_last.get(k, "")
                    if meta["published_at"] > prev:
                        pair_last[k] = meta["published_at"]
                if meta.get("source"):
                    pair_sources[k].add(meta["source"])

    rows = []
    for (a, b), n in pair_count.items():
        if n < args.min_count:
            continue
        rows.append({
            "a": a, "b": b, "count": n,
            "last_seen": pair_last.get((a, b)),
            "sources": list(pair_sources[(a, b)]),
        })

    print(f"[news_corel] pairs total={len(pair_count)} "
          f"≥{args.min_count}={len(rows)}")

    if args.dry_run or not rows:
        for r in rows[:10]:
            print("  ", r)
        return 0

    from autonexusgraph.db.neo4j import get_driver
    with get_driver().session() as session:
        for i in range(0, len(rows), 200):
            session.run(CYPHER, rows=rows[i:i + 200])

    with get_driver().session() as session:
        n = session.run("MATCH ()-[r:CO_MENTIONED_WITH]-() RETURN count(r) AS n").single()["n"]
    print(f"[news_corel] CO_MENTIONED_WITH edges (incl. both directions): {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
