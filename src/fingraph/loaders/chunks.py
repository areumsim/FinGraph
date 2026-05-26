"""vec.chunks 적재 — DART zip → 청킹 → INSERT (embedding optional).

2단계 분리:
- build_chunks.py: zip → 청킹 → text + metadata 만 INSERT (embedding NULL)
- embed_chunks.py: embedding NULL row 만 BGE-M3 호출 → UPDATE

이렇게 분리하는 이유:
- BGE-M3 서버 (GPU) 미가동 환경에서도 적재 가능
- 청킹 로직 변경 시 임베딩 재계산 안 해도 됨
- 임베딩 backend 교체 (BGE-M3 ↔ Azure 등) 시 backfill 단순
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..config import get_settings
from ..extraction import chunk_text, parse_dart_zip
from ._common import LoadStats, chunked


SQL_INSERT_CHUNK = """
INSERT INTO vec.chunks (corp_code, rcept_no, section, chunk_idx, text, token_count, metadata)
VALUES (%(corp_code)s, %(rcept_no)s, %(section)s, %(chunk_idx)s, %(text)s,
        %(token_count)s, %(metadata)s::jsonb)
ON CONFLICT (rcept_no, chunk_idx) DO UPDATE SET
  text        = EXCLUDED.text,
  token_count = EXCLUDED.token_count,
  section     = EXCLUDED.section,
  metadata    = EXCLUDED.metadata
"""


def _iter_zips(bulk_root: Path) -> Iterator[tuple[str, str, Path]]:
    """(corp_code, rcept_no, zip_path) iterator."""
    for corp_dir in sorted(bulk_root.iterdir()):
        if not corp_dir.is_dir():
            continue
        docs = corp_dir / "documents"
        if not docs.exists():
            continue
        for z in sorted(docs.glob("*.zip")):
            yield corp_dir.name, z.stem, z


def _build_rows(corp_code: str, rcept_no: str, zip_path: Path) -> Iterator[dict]:
    """zip 1개 → chunk row dict iterator. global chunk_idx 는 (section, in-section) flatten."""
    try:
        sections = parse_dart_zip(zip_path)
    except Exception as e:
        # 손상 zip, 파싱 실패 → skip
        return iter([])
    rows: list[dict] = []
    global_idx = 0
    for sect in sections:
        chunks = chunk_text(sect.text, section_title=sect.title)
        for ch in chunks:
            rows.append({
                "corp_code": corp_code,
                "rcept_no": rcept_no,
                "section": sect.title[:100],
                "chunk_idx": global_idx,
                "text": ch.text,
                "token_count": ch.token_est,
                "metadata": json.dumps({
                    "section_idx": sect.section_idx,
                    "chunk_in_section": ch.idx,
                    "char_count": ch.char_count,
                }, ensure_ascii=False),
            })
            global_idx += 1
    return iter(rows)


def load_chunks(
    *,
    bulk_root: Path | None = None,
    limit_reports: int | None = None,
    dry_run: bool = False,
    batch_size: int = 500,
    progress: bool = True,
) -> LoadStats:
    """DART zip 순회 → 청킹 → vec.chunks 적재 (embedding NULL).

    Args:
        limit_reports: 처음 N개 zip 만 처리 (smoke test 용).
    """
    s = get_settings()
    bulk_root = bulk_root or (s.ingest_raw_dir / "dart_bulk" / "corp")
    stats = LoadStats()

    if not bulk_root.exists():
        raise FileNotFoundError(f"bulk_root 없음: {bulk_root}")

    # 진행률 — zip 단위 (총 zip 수 측정 위해 한 번 list)
    zips = list(_iter_zips(bulk_root))
    if limit_reports:
        zips = zips[:limit_reports]
    if progress:
        try:
            from tqdm import tqdm
            zips_iter = tqdm(zips, desc="chunks", unit="zip")
        except ImportError:
            zips_iter = zips
    else:
        zips_iter = zips

    # 모든 row 생성 후 배치 INSERT
    def _all_rows() -> Iterator[dict]:
        for corp_code, rcept_no, zip_path in zips_iter:
            yield from _build_rows(corp_code, rcept_no, zip_path)

    if dry_run:
        count = sum(1 for _ in _all_rows())
        stats.inserted = count
        stats.batches = (count + batch_size - 1) // batch_size
        stats.sql_preview.append(SQL_INSERT_CHUNK.strip())
        stats.sql_preview.append(f"-- estimated rows: {count:,} in {stats.batches:,} batches")
        return stats

    from ..db.postgres import transaction
    with transaction() as conn:
        with conn.cursor() as cur:
            for batch in chunked(_all_rows(), batch_size):
                try:
                    cur.executemany(SQL_INSERT_CHUNK, batch)
                    stats.inserted += len(batch)
                    stats.batches += 1
                except Exception:
                    stats.failed += len(batch)
                    raise
    return stats


# ── 임베딩 backfill ────────────────────────────────────────────────
SQL_SELECT_EMPTY = """
SELECT id, text FROM vec.chunks WHERE embedding IS NULL ORDER BY id LIMIT %s
"""

SQL_UPDATE_EMBEDDING = "UPDATE vec.chunks SET embedding = %s WHERE id = %s"


def embed_chunks(
    *,
    batch_size: int = 64,
    progress: bool = True,
) -> LoadStats:
    """embedding IS NULL 인 청크에 BGE-M3 임베딩 채우기.

    BGE-M3 서버가 .env 의 EMBEDDING_URL 에 떠있어야 함.
    """
    from ..embeddings import EmbeddingError, get_embedding_client

    stats = LoadStats()
    client = get_embedding_client()

    # 헬스 체크
    h = client.health()
    if not h.get("embed"):
        raise EmbeddingError("BGE-M3 embed 엔드포인트 미동작. "
                             "scripts/serve_embeddings.py 먼저 기동 또는 EMBEDDING_URL 확인.")

    from ..db.postgres import get_connection, transaction

    # 총 NULL 개수 (진행률용)
    with get_connection().cursor() as cur:
        cur.execute("SELECT count(*) FROM vec.chunks WHERE embedding IS NULL")
        total = cur.fetchone()[0]
    if total == 0:
        return stats

    try:
        from tqdm import tqdm
        pbar = tqdm(total=total, desc="embed", unit="chunk", disable=not progress)
    except ImportError:
        pbar = None

    done = 0
    while True:
        with get_connection().cursor() as cur:
            cur.execute(SQL_SELECT_EMPTY, (batch_size,))
            rows = cur.fetchall()
        if not rows:
            break

        ids = [r[0] for r in rows]
        texts = [r[1] for r in rows]
        try:
            vectors = client.embed(texts)
        except Exception as e:
            stats.failed += len(rows)
            raise EmbeddingError(f"embed batch failed: {e}") from e

        # pgvector 어댑터 — list[float] 또는 numpy 둘 다 OK
        from pgvector.psycopg import register_vector
        with transaction() as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                for rid, vec in zip(ids, vectors):
                    cur.execute(SQL_UPDATE_EMBEDDING, (vec, rid))

        stats.inserted += len(rows)
        stats.batches += 1
        done += len(rows)
        if pbar:
            pbar.update(len(rows))
    if pbar:
        pbar.close()
    return stats
