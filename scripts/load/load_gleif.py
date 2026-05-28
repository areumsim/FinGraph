#!/usr/bin/env python3
"""GLEIF KR LEI → sec.lei + master.entity_map (legal_name 매칭).

매칭 전략:
- legal_name 정규화 후 master.companies / company_aliases 와 매칭
- 매치되면 corp_code 채움. 못 찾으면 corp_code NULL (외부 법인)
- LEI 자체는 모두 sec.lei 에 저장 (검색용)
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
from autonexusgraph.ingestion._common import normalize_corp_name


UPSERT_LEI = """
INSERT INTO sec.lei
  (lei, corp_code, legal_name, legal_jurisdiction, entity_status,
   registration_status, issued_at, next_renewal_at, raw)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (lei) DO UPDATE
   SET corp_code           = COALESCE(EXCLUDED.corp_code, sec.lei.corp_code),
       legal_name          = EXCLUDED.legal_name,
       entity_status       = EXCLUDED.entity_status,
       registration_status = EXCLUDED.registration_status,
       next_renewal_at     = EXCLUDED.next_renewal_at
"""

UPSERT_EM = """
INSERT INTO master.entity_map
  (corp_code, id_type, id_value, source, confidence, resolved_by)
VALUES (%s, 'lei', %s, 'gleif', 0.95, 'rule')
ON CONFLICT (corp_code, id_type, id_value) DO UPDATE
   SET confidence  = GREATEST(master.entity_map.confidence, EXCLUDED.confidence),
       resolved_at = now()
"""


def _parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s.split("T")[0])
    except (ValueError, AttributeError):
        return None


def _load_name_index(pool) -> dict[str, str]:
    """alias_norm → corp_code."""
    idx: dict[str, str] = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT alias_norm, corp_code FROM master.company_aliases")
        for k, cc in cur.fetchall():
            idx.setdefault(k, cc)
        cur.execute("SELECT corp_code, corp_name FROM master.companies WHERE is_active=TRUE")
        for cc, nm in cur.fetchall():
            idx.setdefault(normalize_corp_name(nm), cc)
    return idx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    path = s.ingest_raw_dir / "gleif" / "kr_records.json"
    if not path.exists():
        print(f"{path} 없음 — download_gleif.py 먼저", file=sys.stderr)
        return 2

    pool = get_pool()
    name_idx = _load_name_index(pool)
    print(f"[load_gleif] alias index: {len(name_idx)}")

    records = json.loads(path.read_text(encoding="utf-8"))
    print(f"[load_gleif] records: {len(records)}")

    lei_rows: list[tuple] = []
    em_rows: list[tuple] = []
    matched = 0
    for r in records:
        legal = r.get("legal_name") or ""
        key = normalize_corp_name(legal)
        corp_code = name_idx.get(key)
        if corp_code:
            matched += 1
            em_rows.append((corp_code, r["lei"]))

        lei_rows.append((
            r["lei"],
            corp_code,
            legal[:300] if legal else None,
            r.get("jurisdiction"),
            r.get("entity_status"),
            r.get("registration_status"),
            _parse_date(r.get("issued_at")),
            _parse_date(r.get("next_renewal_at")),
            json.dumps(r, ensure_ascii=False),
        ))

    print(f"[load_gleif] matched to corp_code: {matched}/{len(records)}")
    if args.dry_run:
        return 0

    with pool.connection() as conn, conn.cursor() as cur:
        BATCH = 500
        for i in range(0, len(lei_rows), BATCH):
            cur.executemany(UPSERT_LEI, lei_rows[i:i + BATCH])
        for i in range(0, len(em_rows), BATCH):
            cur.executemany(UPSERT_EM, em_rows[i:i + BATCH])

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sec.lei")
        print(f"[sec.lei] total: {cur.fetchone()[0]:,}")
        cur.execute("SELECT count(*) FROM sec.lei WHERE corp_code IS NOT NULL")
        print(f"[sec.lei] matched to corp: {cur.fetchone()[0]:,}")
        cur.execute("SELECT count(*) FROM master.entity_map WHERE id_type='lei'")
        print(f"[entity_map] lei rows: {cur.fetchone()[0]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
