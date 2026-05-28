"""fin.filings 적재 — DART 공시 보고서 메타.

원본:
- data/raw/dart_bulk/corp/<corp_code>/filings.jsonl

PK: fin.filings.rcept_no (DART 접수번호)
upsert: 메타 정보 변경 가능
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import get_settings
from ._common import LoadStats, chunked, iter_jsonl, parse_date


SQL_UPSERT = """
INSERT INTO fin.filings
  (rcept_no, corp_code, report_nm, rcept_dt, flr_nm, pblntf_ty, raw, ingested_at)
VALUES
  (%(rcept_no)s, %(corp_code)s, %(report_nm)s, %(rcept_dt)s,
   %(flr_nm)s, %(pblntf_ty)s, %(raw)s::jsonb, now())
ON CONFLICT (rcept_no) DO UPDATE SET
  report_nm = EXCLUDED.report_nm,
  rcept_dt = EXCLUDED.rcept_dt,
  flr_nm = EXCLUDED.flr_nm,
  pblntf_ty = EXCLUDED.pblntf_ty,
  raw = EXCLUDED.raw,
  ingested_at = now()
"""


def _build_row(corp_code: str, row: dict) -> dict | None:
    rcept_no = row.get("rcept_no")
    if not rcept_no:
        return None
    return {
        "rcept_no": rcept_no,
        "corp_code": corp_code,
        "report_nm": (row.get("report_nm") or "")[:300],
        "rcept_dt": parse_date(row.get("rcept_dt")),
        "flr_nm": (row.get("flr_nm") or "")[:200],
        "pblntf_ty": (row.get("pblntf_ty") or "")[:1] or None,
        "raw": json.dumps(row, ensure_ascii=False),
    }


def load_filings(
    *,
    bulk_root: Path | None = None,
    dry_run: bool = False,
    batch_size: int = 1000,
) -> LoadStats:
    """전 corp 의 filings.jsonl 일괄 upsert."""
    s = get_settings()
    bulk_root = bulk_root or (s.ingest_raw_dir / "dart_bulk" / "corp")
    stats = LoadStats()

    if not bulk_root.exists():
        raise FileNotFoundError(f"bulk_root 없음: {bulk_root}")

    def _iter_all() -> "list[dict]":
        rows = []
        for corp_dir in sorted(bulk_root.iterdir()):
            if not corp_dir.is_dir():
                continue
            corp_code = corp_dir.name
            for row in iter_jsonl(corp_dir / "filings.jsonl"):
                built = _build_row(corp_code, row)
                if built is None:
                    continue
                rows.append(built)
        return rows

    rows = _iter_all()

    if dry_run:
        stats.inserted = len(rows)
        stats.batches = (len(rows) + batch_size - 1) // batch_size
        if rows:
            stats.sql_preview.append(SQL_UPSERT.strip())
            stats.sql_preview.append(
                f"-- total rows: {len(rows):,}, sample: {rows[0]['rcept_no']} ({rows[0]['report_nm'][:60]})"
            )
        return stats

    from ..db.postgres import transaction
    with transaction() as conn:
        with conn.cursor() as cur:
            for batch in chunked(iter(rows), batch_size):
                try:
                    cur.executemany(SQL_UPSERT, batch)
                    stats.inserted += len(batch)
                    stats.batches += 1
                except Exception:
                    stats.failed += len(batch)
                    raise
    return stats
