#!/usr/bin/env python3
"""Wikipedia 본문 HTML → 텍스트 추출 → vec.chunks 적재 (embedding NULL).

DART 청크와 같은 테이블, section='wikipedia_<lang>' 으로 식별.
rcept_no 컬럼은 NULL 허용 → Wikipedia 청크는 rcept_no=NULL 로.
metadata 에 wikipedia_title, lang 보관.

대상: data/raw/wikipedia/<lang>/<corp_code>/page.html

사용:
    python scripts/load/build_chunks_wikipedia.py [--lang ko] [--limit N]
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


SQL_DELETE_PREV = """
DELETE FROM vec.chunks
 WHERE corp_code = %s AND section = %s AND rcept_no IS NULL
"""

# Wikipedia 청크는 rcept_no NULL. PG 의 (rcept_no, chunk_idx) UNIQUE 는 NULL 을 distinct 로 봐
# 충돌이 안 나지만, 재실행 멱등성을 위해 같은 (corp_code, section) 전체를 DELETE 후 INSERT.
SQL_INSERT = """
INSERT INTO vec.chunks
  (corp_code, rcept_no, section, chunk_idx, text, token_count, metadata,
   source, fiscal_year, report_type)
VALUES (%(corp_code)s, NULL, %(section)s, %(chunk_idx)s, %(text)s,
        %(token_count)s, %(metadata)s::jsonb,
        'wikipedia', NULL, 'wikipedia')
"""


def _html_to_text(html: str) -> str:
    """간단 HTML → text. <p>/<li> 는 줄바꿈."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # fallback — naive
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        return " ".join(text.split())
    soup = BeautifulSoup(html, "html.parser")
    # script/style 제거
    for tag in soup(["script", "style", "table", "sup", "img"]):
        tag.decompose()
    # 단락 구분
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text(separator="\n")
    # 연속 빈줄 정리
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _chunk_text_simple(text: str, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    """문단 단위 청킹 + char 기반 윈도우. 한국어 문장 분리는 단순."""
    paras = [p for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if len(cur) + len(p) + 1 <= max_chars:
            cur = f"{cur}\n\n{p}".strip() if cur else p
        else:
            if cur:
                chunks.append(cur)
            # 긴 문단은 강제 분할
            if len(p) > max_chars:
                for i in range(0, len(p), max_chars - overlap):
                    chunks.append(p[i:i + max_chars])
                cur = ""
            else:
                cur = p
    if cur:
        chunks.append(cur)
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="ko")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    root = s.ingest_raw_dir / "wikipedia" / args.lang
    if not root.exists():
        print(f"{root} 없음", file=sys.stderr)
        return 2

    targets = sorted([d for d in root.iterdir() if d.is_dir()])
    if args.limit:
        targets = targets[:args.limit]

    section_key = f"wikipedia_{args.lang}"
    print(f"[wiki_chunks] targets: {len(targets)} section='{section_key}'")

    total_chunks = 0
    total_corps_ok = 0
    pool = get_pool()

    try:
        from tqdm import tqdm
        iterator = tqdm(targets, desc="wiki", unit="corp")
    except ImportError:
        iterator = targets

    for corp_dir in iterator:
        corp_code = corp_dir.name
        html_path = corp_dir / "page.html"
        meta_path = corp_dir / "meta.json"
        if not html_path.exists():
            continue
        try:
            html = html_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        text = _html_to_text(html)
        if len(text) < 200:
            continue
        chunks = _chunk_text_simple(text)
        if not chunks:
            continue
        total_corps_ok += 1
        total_chunks += len(chunks)
        if args.dry_run:
            continue
        rows = []
        for idx, ch in enumerate(chunks):
            rows.append({
                "corp_code": corp_code,
                "section": section_key,
                "chunk_idx": idx,
                "text": ch,
                "token_count": len(ch) // 3,  # 한국어 대략 1 token ≈ 3 chars
                "metadata": json.dumps({
                    "source": "wikipedia",
                    "lang": args.lang,
                    "title": meta.get("title"),
                    "revision_id": meta.get("revision_id"),
                }, ensure_ascii=False),
            })
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(SQL_DELETE_PREV, (corp_code, section_key))
            cur.executemany(SQL_INSERT, rows)

    print(f"\n[wiki_chunks] corps_with_chunks={total_corps_ok} total_chunks={total_chunks:,}")

    # 검증
    if not args.dry_run:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT section, count(*) FROM vec.chunks
                 WHERE section LIKE 'wikipedia_%'
                 GROUP BY section ORDER BY 2 DESC
            """)
            for r in cur.fetchall():
                print(f"[vec.chunks] {r[0]} : {r[1]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
