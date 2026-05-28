#!/usr/bin/env python3
"""KCGS ESG 등급 CSV → esg.ratings.

KCGS 는 공식 API 없음. 매년 공시되는 등급표 CSV 를 수동 다운로드 후 적재.

저장 형식 (data/raw/kcgs/<year>/ratings.csv 예시 헤더):
  회사명, 종목코드, 환경, 사회, 지배구조, 종합
  삼성전자, 005930, A+, A, A, A+
  ...

사용:
    python scripts/load/load_kcgs.py --year 2024
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_pool
from autonexusgraph.ingestion._common import normalize_corp_name


UPSERT_ESG = """
INSERT INTO esg.ratings
  (corp_code, year, source, e_grade, s_grade, g_grade, total_grade, raw)
VALUES (%s, %s, 'kcgs', %s, %s, %s, %s, %s)
ON CONFLICT (corp_code, year, source) DO UPDATE
   SET e_grade     = EXCLUDED.e_grade,
       s_grade     = EXCLUDED.s_grade,
       g_grade     = EXCLUDED.g_grade,
       total_grade = EXCLUDED.total_grade,
       raw         = EXCLUDED.raw,
       ingested_at = now()
"""


COL_MAP_TOKENS = {
    "회사명": "name", "기업명": "name", "company": "name",
    "종목코드": "ticker", "코드": "ticker",
    "환경": "e", "e등급": "e", "환경등급": "e",
    "사회": "s", "s등급": "s", "사회등급": "s",
    "지배구조": "g", "g등급": "g",
    "종합": "total", "통합": "total", "종합등급": "total",
}


def _detect_columns(header: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, h in enumerate(header):
        k = h.strip().lower()
        for tok, std in COL_MAP_TOKENS.items():
            if tok.lower() in k:
                out.setdefault(std, i)
                break
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--csv", type=Path, default=None,
                        help="기본: data/raw/kcgs/<year>/ratings.csv")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    csv_path = args.csv or (s.ingest_raw_dir / "kcgs" / str(args.year) / "ratings.csv")
    if not csv_path.exists():
        print(f"{csv_path} 없음.")
        print("\n[수동 다운로드 가이드]")
        print("  KCGS 매년 ESG 등급 공시 (보통 10~12월) — 보도자료 또는 등급조회 페이지에서 등급표 다운")
        print(f"  저장: {csv_path}")
        print("  헤더: 회사명,종목코드,환경,사회,지배구조,종합")
        return 1

    pool = get_pool()
    # ticker → corp_code
    t2c: dict[str, str] = {}
    n2c: dict[str, str] = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT em.id_value AS ticker, em.corp_code
              FROM master.entity_map em
             WHERE em.id_type = 'ticker'
        """)
        for t, cc in cur.fetchall():
            if t:
                t2c[t.strip().zfill(6)] = cc
        cur.execute("SELECT corp_code, corp_name FROM master.companies WHERE is_active=TRUE")
        for cc, nm in cur.fetchall():
            n2c[normalize_corp_name(nm)] = cc

    rows: list[tuple] = []
    unmatched = 0
    with csv_path.open(encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        cols = _detect_columns(header)
        print(f"[KCGS] columns detected: {cols}")
        for r in reader:
            try:
                name = r[cols["name"]].strip() if "name" in cols else ""
                ticker = r[cols["ticker"]].strip() if "ticker" in cols else ""
                e = r[cols["e"]].strip() if "e" in cols else None
                soc = r[cols["s"]].strip() if "s" in cols else None
                g = r[cols["g"]].strip() if "g" in cols else None
                total = r[cols["total"]].strip() if "total" in cols else None
            except (IndexError, KeyError):
                continue

            corp_code = None
            if ticker:
                corp_code = t2c.get(ticker.zfill(6))
            if not corp_code and name:
                corp_code = n2c.get(normalize_corp_name(name))
            if not corp_code:
                unmatched += 1
                continue

            rows.append((
                corp_code, args.year, e or None, soc or None, g or None, total or None,
                json.dumps({"raw": r, "header": header}, ensure_ascii=False),
            ))

    print(f"[KCGS] rows={len(rows)} unmatched={unmatched}")
    if args.dry_run:
        for r in rows[:5]:
            print("  ", r[:5])
        return 0

    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(UPSERT_ESG, rows)
    print(f"[esg.ratings] upserted {len(rows)} rows for year={args.year}")

    # Neo4j Company 속성에 등급 반영 (멱등 — SET).
    # Cypher 의 SET 은 dynamic property name 미지원 → APOC.create.setProperty 사용.
    # APOC 가 없을 경우 fallback: year 마다 별도 cypher.
    neo_rows = [
        {"corp_code": r[0],
         "e": r[2], "s": r[3], "g": r[4], "total": r[5]}
        for r in rows
    ]
    if neo_rows:
        from autonexusgraph.db.neo4j import get_driver
        year_key = f"esg_{args.year}"
        cypher = f"""
        UNWIND $rows AS r
        MATCH (c:Company {{corp_code: r.corp_code}})
        SET c.{year_key}_e     = r.e,
            c.{year_key}_s     = r.s,
            c.{year_key}_g     = r.g,
            c.{year_key}_total = r.total
        """
        with get_driver().session() as session:
            session.run(cypher, rows=neo_rows)
        print(f"[neo4j] Company.{year_key}_* 속성 {len(neo_rows)}개 노드에 SET")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
