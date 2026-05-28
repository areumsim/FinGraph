#!/usr/bin/env python3
"""SEC EDGAR raw → PG sec.filings + sec.lei (LEI 매핑은 별도 GLEIF).

각 회사 폴더의 submissions.json 에서 filings.recent 추출.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_pool
from autonexusgraph.ingestion.sec_client import SecEdgarClient, SecFiling


UPSERT_FILING = """
INSERT INTO sec.filings
  (accession_no, cik, corp_code, company_name, form_type,
   filed_at, period_of_report, primary_doc_url, raw)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (accession_no) DO UPDATE
   SET corp_code = COALESCE(EXCLUDED.corp_code, sec.filings.corp_code),
       form_type = EXCLUDED.form_type,
       filed_at  = EXCLUDED.filed_at,
       period_of_report = EXCLUDED.period_of_report
"""


def _parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _cik_to_corp(pool) -> dict[str, str]:
    out = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT corp_code, id_value FROM master.entity_map WHERE id_type='cik'")
        for cc, cik in cur.fetchall():
            out[str(cik).zfill(10)] = cc
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    sec_root = s.ingest_raw_dir / "sec"
    if not sec_root.exists():
        print(f"{sec_root} 없음", file=sys.stderr)
        return 2

    pool = get_pool()
    cik_to_corp = _cik_to_corp(pool)
    print(f"[load_sec] cik→corp map: {len(cik_to_corp)}")

    rows: list[tuple] = []
    for cik_dir in sorted(sec_root.iterdir()):
        if not cik_dir.is_dir():
            continue
        sub_path = cik_dir / "submissions.json"
        if not sub_path.exists():
            continue
        try:
            sub = json.loads(sub_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        cik = str(sub.get("cik", cik_dir.name)).zfill(10)
        corp_code = cik_to_corp.get(cik)
        company_name = sub.get("name")
        # extract via client helper
        client = SecEdgarClient.__new__(SecEdgarClient)
        # avoid __init__ — only need extract_filings
        filings: list[SecFiling] = []
        for f in SecEdgarClient.extract_filings(client, sub):
            rows.append((
                f.accession_no, f.cik, corp_code, company_name,
                f.form_type, _parse_date(f.filed_at),
                _parse_date(f.period_of_report),
                f.primary_doc_url,
                json.dumps({"accession_no": f.accession_no, "form": f.form_type},
                           ensure_ascii=False),
            ))

    print(f"[load_sec] filings rows: {len(rows)}")
    if args.dry_run:
        for r in rows[:5]:
            print("  ", r)
        return 0

    with pool.connection() as conn, conn.cursor() as cur:
        BATCH = 500
        for i in range(0, len(rows), BATCH):
            cur.executemany(UPSERT_FILING, rows[i:i + BATCH])

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sec.filings")
        n = cur.fetchone()[0]
        cur.execute("SELECT form_type, count(*) FROM sec.filings GROUP BY form_type ORDER BY 2 DESC LIMIT 10")
        forms = cur.fetchall()
    print(f"[sec.filings] total: {n}")
    print("[sec.filings] top form types:")
    for r in forms:
        print(f"  {r[0] or 'NULL':10s} {r[1]:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
