#!/usr/bin/env python3
"""Wikipedia 적재:
  - wiki.wikipedia_pages : title / page_id / extract / infobox
  - master.company_aliases : Wikipedia title 자체를 alias 로
  - Neo4j Company 속성 : wikipedia_title_ko, wikipedia_url

본문 HTML 은 별도 청킹 단계 (Step 10) 에서 vec.chunks 로 적재.
이 loader 는 메타데이터만 처리 — 작고 빠름.

사용:
    python scripts/load/load_wikipedia.py [--lang ko|en] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_pool
from autonexusgraph.ingestion._common import normalize_corp_name


UPSERT_PAGE = """
INSERT INTO wiki.wikipedia_pages
  (corp_code, lang, title, page_id, revision_id, extract, infobox, last_modified)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (corp_code, lang) DO UPDATE
   SET title       = EXCLUDED.title,
       page_id     = EXCLUDED.page_id,
       revision_id = EXCLUDED.revision_id,
       extract     = EXCLUDED.extract,
       infobox     = EXCLUDED.infobox,
       last_modified = EXCLUDED.last_modified,
       ingested_at = now()
"""

UPSERT_ALIAS = """
INSERT INTO master.company_aliases (alias, alias_norm, corp_code, source, confidence)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (alias_norm, corp_code, source) DO UPDATE
   SET confidence = EXCLUDED.confidence
"""

UPSERT_EM = """
INSERT INTO master.entity_map (corp_code, id_type, id_value, source, confidence, resolved_by)
VALUES (%s, %s, %s, 'wikipedia', 0.95, 'rule')
ON CONFLICT (corp_code, id_type, id_value) DO UPDATE
   SET resolved_at = now()
"""

NEO4J_UPSERT = """
UNWIND $rows AS r
MATCH (c:Company {corp_code: r.corp_code})
SET c.wikipedia_title_ko = r.title,
    c.wikipedia_url_ko   = r.url
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="ko")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-neo4j", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    root = s.ingest_raw_dir / "wikipedia" / args.lang

    pages: list[tuple] = []
    aliases: list[tuple] = []
    em_rows: list[tuple] = []
    neo4j_rows: list[dict] = []

    if not root.exists():
        print(f"{root} 없음 — Wikipedia 수집 먼저 실행하세요.", file=sys.stderr)
        return 2

    for corp_dir in sorted(root.iterdir()):
        if not corp_dir.is_dir():
            continue
        corp_code = corp_dir.name
        meta_path = corp_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        infobox = None
        ib_path = corp_dir / "infobox.json"
        if ib_path.exists():
            try:
                infobox = json.loads(ib_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

        title = meta.get("title")
        if not title:
            continue

        # Wikipedia 의 timestamp 는 ISO8601 like "2024-12-19T08:30:00Z" — 그대로 저장
        last_mod = meta.get("last_modified")

        pages.append((
            corp_code, args.lang, title, meta.get("page_id"),
            meta.get("revision_id"), meta.get("extract"),
            json.dumps(infobox, ensure_ascii=False) if infobox else None,
            last_mod,
        ))

        aliases.append((title, normalize_corp_name(title), corp_code, "wikipedia", 0.90))
        em_rows.append((corp_code, "wikipedia_title", title[:200]))
        neo4j_rows.append({
            "corp_code": corp_code,
            "title": title,
            "url": f"https://{args.lang}.wikipedia.org/wiki/{title.replace(' ', '_')}",
        })

    print(f"[load_wikipedia] pages={len(pages)} aliases={len(aliases)} neo4j={len(neo4j_rows)}")

    if args.dry_run:
        for p in pages[:3]:
            print("  P:", p[:5])
        return 0

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        BATCH = 200
        for i in range(0, len(pages), BATCH):
            cur.executemany(UPSERT_PAGE, pages[i:i + BATCH])
        for i in range(0, len(aliases), BATCH):
            cur.executemany(UPSERT_ALIAS, aliases[i:i + BATCH])
        for i in range(0, len(em_rows), BATCH):
            cur.executemany(UPSERT_EM, em_rows[i:i + BATCH])

    if not args.no_neo4j and neo4j_rows:
        from autonexusgraph.db.neo4j import get_driver
        with get_driver().session() as session:
            for i in range(0, len(neo4j_rows), 200):
                session.run(NEO4J_UPSERT, rows=neo4j_rows[i:i + 200])

    # 검증
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT lang, count(*) FROM wiki.wikipedia_pages GROUP BY lang")
        for r in cur.fetchall():
            print(f"[wikipedia_pages] {r[0]}: {r[1]:,}")
        cur.execute("""
            SELECT count(*) FROM master.entity_map WHERE id_type='wikipedia_title'
        """)
        print(f"[entity_map] wikipedia_title: {cur.fetchone()[0]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
