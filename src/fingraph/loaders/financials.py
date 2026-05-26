"""fin.financials 적재 — DART XBRL fnlttSinglAcntAll.

원본:
- data/raw/dart_bulk/corp/<corp_code>/financials/<year>_annual_CFS.jsonl

UNIQUE: (corp_code, bsns_year, reprt_code, fs_div, account_id, account_nm)

대량(184K+)이라 `executemany` (batch 1000) 사용.
COPY ... FROM 도 가능하지만 ON CONFLICT 처리 위해 임시테이블 + INSERT...SELECT 필요 →
복잡도 대비 이득 작음. executemany 로 충분 (몇 분 소요).

성능 팁:
- COMMIT 간격을 잘 잡으면 더 빨라짐 (배치 단위 commit 또는 전체 1회)
- 현재는 전체 트랜잭션 1회 commit → 안전, 중간 실패 시 전체 롤백
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from ..config import get_settings
from ._common import LoadStats, chunked, iter_jsonl, parse_amount, parse_int


SQL_UPSERT = """
INSERT INTO fin.financials
  (corp_code, bsns_year, reprt_code, fs_div, sj_div, account_id, account_nm,
   thstrm_amount, frmtrm_amount, bfefrmtrm_amount, ord, raw)
VALUES
  (%(corp_code)s, %(bsns_year)s, %(reprt_code)s, %(fs_div)s, %(sj_div)s,
   %(account_id)s, %(account_nm)s, %(thstrm_amount)s, %(frmtrm_amount)s,
   %(bfefrmtrm_amount)s, %(ord)s, %(raw)s::jsonb)
ON CONFLICT (corp_code, bsns_year, reprt_code, fs_div, account_id, account_nm)
DO UPDATE SET
  thstrm_amount = EXCLUDED.thstrm_amount,
  frmtrm_amount = EXCLUDED.frmtrm_amount,
  bfefrmtrm_amount = EXCLUDED.bfefrmtrm_amount,
  ord = EXCLUDED.ord,
  raw = EXCLUDED.raw
"""


def _build_row(corp_code: str, year: int, row: dict) -> dict | None:
    """JSONL row → fin.financials row dict."""
    # UNIQUE 키 account_id 는 NULL 허용이지만 다른 컬럼과 함께 정합성 보장 위해 '' 대신 NULL 처리.
    account_nm = (row.get("account_nm") or "").strip()
    if not account_nm:
        return None
    return {
        "corp_code": corp_code,
        "bsns_year": parse_int(row.get("bsns_year")) or year,
        "reprt_code": (row.get("reprt_code") or "11011"),
        "fs_div": (row.get("fs_div") or "CFS"),
        "sj_div": (row.get("sj_div") or "")[:10],
        "account_id": (row.get("account_id") or "")[:100] or None,
        "account_nm": account_nm[:200],
        "thstrm_amount": parse_amount(row.get("thstrm_amount")),
        "frmtrm_amount": parse_amount(row.get("frmtrm_amount")),
        "bfefrmtrm_amount": parse_amount(row.get("bfefrmtrm_amount")),
        "ord": parse_int(row.get("ord")),
        "raw": json.dumps(row, ensure_ascii=False),
    }


def _iter_rows(bulk_root: Path) -> Iterator[dict]:
    """전 corp / 전 연도 / 전 row stream — 메모리 절약 (184K 전체 안 들고)."""
    for corp_dir in sorted(bulk_root.iterdir()):
        if not corp_dir.is_dir():
            continue
        corp_code = corp_dir.name
        fin_dir = corp_dir / "financials"
        if not fin_dir.exists():
            continue
        for fp in sorted(fin_dir.glob("*_annual_CFS.jsonl")):
            try:
                year = int(fp.stem.split("_")[0])
            except ValueError:
                continue
            for row in iter_jsonl(fp):
                built = _build_row(corp_code, year, row)
                if built is None:
                    continue
                yield built


def load_financials(
    *,
    bulk_root: Path | None = None,
    dry_run: bool = False,
    batch_size: int = 1000,
    progress: bool = True,
) -> LoadStats:
    """전 corp 의 재무 row 일괄 upsert.

    Args:
        batch_size: executemany 배치 크기. PG 입장에선 1,000~5,000 권장.
        progress: tqdm 표시 (지원 환경에서만).
    """
    s = get_settings()
    bulk_root = bulk_root or (s.ingest_raw_dir / "dart_bulk" / "corp")
    stats = LoadStats()

    if not bulk_root.exists():
        raise FileNotFoundError(f"bulk_root 없음: {bulk_root}")

    if dry_run:
        # 전체 count 만 계산
        count = sum(1 for _ in _iter_rows(bulk_root))
        stats.inserted = count
        stats.batches = (count + batch_size - 1) // batch_size
        stats.sql_preview.append(SQL_UPSERT.strip())
        stats.sql_preview.append(f"-- estimated rows: {count:,} in {stats.batches:,} batches")
        return stats

    # 진행률 (필수 아님)
    try:
        from tqdm import tqdm
        wrap = lambda it: tqdm(it, desc="financials", unit="batch") if progress else it
    except ImportError:
        wrap = lambda it: it

    from ..db.postgres import transaction
    with transaction() as conn:
        with conn.cursor() as cur:
            for batch in wrap(chunked(_iter_rows(bulk_root), batch_size)):
                try:
                    cur.executemany(SQL_UPSERT, batch)
                    stats.inserted += len(batch)
                    stats.batches += 1
                except Exception:
                    stats.failed += len(batch)
                    raise
    return stats
