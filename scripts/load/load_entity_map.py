#!/usr/bin/env python3
"""master.entity_map + master.company_aliases 초기 시드 적재.

소스:
- master.companies (이미 적재된 295개) → corp_code ↔ ticker, business_no, jurir_no
- corp_name 자체와 normalize_corp_name() 결과 → company_aliases

이후 Wikidata / Wikipedia / GLEIF / SEC 등 외부 ID 는 각 source 별 loader 가 추가.

사용:
    python scripts/load/load_entity_map.py [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# src/ 를 path 에 추가 (Makefile 에서도 PYTHONPATH 안 쓰는 경우 대비)
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.db.postgres import get_pool
from autonexusgraph.ingestion._common import normalize_corp_name


SELECT_COMPANIES = """
SELECT corp_code, corp_name, stock_code, extra
  FROM master.companies
 WHERE is_active = TRUE
"""

UPSERT_ENTITY_MAP = """
INSERT INTO master.entity_map
  (corp_code, id_type, id_value, source, confidence, resolved_by)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (corp_code, id_type, id_value) DO UPDATE
   SET confidence  = EXCLUDED.confidence,
       resolved_at = now(),
       resolved_by = EXCLUDED.resolved_by
"""

UPSERT_ALIAS = """
INSERT INTO master.company_aliases
  (alias, alias_norm, corp_code, source, confidence)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (alias_norm, corp_code, source) DO UPDATE
   SET alias      = EXCLUDED.alias,
       confidence = EXCLUDED.confidence
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="SQL 출력만, 적재 X")
    parser.add_argument("--force", action="store_true", help="(예약) 기존 매핑 무시하고 재적재")
    args = parser.parse_args()

    pool = get_pool()
    em_count = 0
    alias_count = 0
    rows: list[tuple] = []

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(SELECT_COMPANIES)
        rows = cur.fetchall()

    print(f"[entity_map] companies={len(rows)} active rows")

    em_batch: list[tuple] = []
    alias_batch: list[tuple] = []

    for corp_code, corp_name, stock_code, extra in rows:
        cc = corp_code.strip() if corp_code else None
        if not cc:
            continue

        # 1) ticker ←→ corp_code (KRX)
        if stock_code:
            em_batch.append((cc, "ticker", stock_code.strip(), "krx", 1.000, "rule"))

        # 2) corp_code 본인 (self-mapping — 다른 source 가 corp_code 만 알 때 정합성 체크용)
        em_batch.append((cc, "corp_code", cc, "dart", 1.000, "rule"))

        # 3) business_no / jurir_no (extra JSONB 에 있을 수 있음)
        if isinstance(extra, dict):
            for key, id_type in [
                ("bizr_no", "business_no"),
                ("jurir_no", "jurir_no"),
                ("homepage_url", "homepage_url"),
            ]:
                val = extra.get(key)
                if val and str(val).strip():
                    em_batch.append((cc, id_type, str(val).strip(), "dart", 1.000, "rule"))

        # 4) alias 사전 — 원본명 + 정규화
        if corp_name:
            alias_batch.append((corp_name, normalize_corp_name(corp_name), cc, "dart", 1.000))

    print(f"[entity_map] prepared {len(em_batch)} mappings, {len(alias_batch)} aliases")

    if args.dry_run:
        print("--dry-run: showing first 5 of each")
        for r in em_batch[:5]:
            print("  EM:", r)
        for r in alias_batch[:5]:
            print("  AL:", r)
        return 0

    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(UPSERT_ENTITY_MAP, em_batch)
        em_count = len(em_batch)
        cur.executemany(UPSERT_ALIAS, alias_batch)
        alias_count = len(alias_batch)

    # 적재 결과 검증
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id_type, count(*) FROM master.entity_map GROUP BY id_type ORDER BY 1")
        em_breakdown = cur.fetchall()
        cur.execute("SELECT count(*) FROM master.company_aliases")
        alias_total = cur.fetchone()[0]

    print(f"\n[entity_map] upserted {em_count} rows. by id_type:")
    for it, n in em_breakdown:
        print(f"  {it:20s} {n}")
    print(f"[company_aliases] total rows: {alias_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
