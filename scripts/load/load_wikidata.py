#!/usr/bin/env python3
"""Wikidata 적재:
  - master.entity_map (wikidata_qid / isin / lei / cik / homepage / sec_ticker)
  - wiki.wikidata_facts (모든 statement raw 보관)
  - Neo4j Company 노드 속성 보강 (wikidata_qid / inception / hq / industry)

선행: data/raw/wikidata/matched.jsonl, data/raw/wikidata/entities/<qid>.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_pool
from autonexusgraph.ingestion.wikidata_client import (
    claim_qid_values, claim_string_values, claim_values,
)


UPSERT_EM = """
INSERT INTO master.entity_map
  (corp_code, id_type, id_value, source, confidence, resolved_by, notes)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (corp_code, id_type, id_value) DO UPDATE
   SET confidence  = GREATEST(master.entity_map.confidence, EXCLUDED.confidence),
       resolved_at = now(),
       resolved_by = EXCLUDED.resolved_by
"""

UPSERT_FACT = """
INSERT INTO wiki.wikidata_facts
  (corp_code, qid, property, value, value_type, value_qid, raw)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (corp_code, qid, property, value) DO UPDATE
   SET value_type = EXCLUDED.value_type,
       value_qid  = EXCLUDED.value_qid,
       raw        = EXCLUDED.raw
"""

# Wikidata 주요 property → entity_map id_type
PROPERTY_MAP = {
    "P249":  ("ticker",      "ticker symbol"),
    "P946":  ("isin",        "ISIN"),
    "P1278": ("lei",         "LEI"),
    "P5531": ("cik",         "SEC CIK"),
    "P856":  ("homepage",    "official website"),
    "P3220": ("kr_legal_no", "Korea legal entity no"),
}


# Neo4j Company 속성 보강 Cypher (멱등 — MERGE 가 아닌 SET only)
NEO4J_UPSERT = """
UNWIND $rows AS r
MATCH (c:Company {corp_code: r.corp_code})
SET c.wikidata_qid = r.qid,
    c.label_en     = coalesce(r.label_en, c.label_en),
    c.inception    = coalesce(r.inception, c.inception),
    c.headquarters = coalesce(r.headquarters, c.headquarters),
    c.industry_wd  = coalesce(r.industry, c.industry_wd)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-neo4j", action="store_true")
    args = parser.parse_args()

    s = get_settings()
    matched_path = s.ingest_raw_dir / "wikidata" / "matched.jsonl"
    entities_dir = s.ingest_raw_dir / "wikidata" / "entities"

    if not matched_path.exists():
        print("matched.jsonl 없음", file=sys.stderr)
        return 2

    matches: list[dict] = []
    with matched_path.open(encoding="utf-8") as f:
        for line in f:
            matches.append(json.loads(line))
    print(f"[load_wikidata] matched: {len(matches)}")

    em_rows: list[tuple] = []
    fact_rows: list[tuple] = []
    neo4j_rows: list[dict] = []

    for m in matches:
        corp_code = m["corp_code"]
        qid = m["qid"]
        confidence = float(m.get("confidence", 0.85))

        # 1) entity_map: wikidata_qid 자체
        em_rows.append((
            corp_code, "wikidata_qid", qid, "wikidata", confidence, "rule",
            f"match_by={m.get('match_by','?')}",
        ))

        ent_path = entities_dir / f"{qid}.json"
        if not ent_path.exists():
            continue

        try:
            entity = json.loads(ent_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        # 2) 표준 ID 들 (claims 기반)
        for pid, (id_type, _desc) in PROPERTY_MAP.items():
            for v in claim_string_values(entity, pid):
                em_rows.append((
                    corp_code, id_type, v[:200], "wikidata", confidence, "rule",
                    f"property={pid}",
                ))
                fact_rows.append((
                    corp_code, qid, pid, v[:1000], "string", None,
                    json.dumps({"property": pid, "value": v}, ensure_ascii=False),
                ))

        # 3) Neo4j 속성 — inception(P571), headquarters(P159), industry(P452)
        # inception: time 값
        inception_val = None
        for cv in claim_values(entity, "P571"):
            v = cv["value"]
            if isinstance(v, dict) and "time" in v:
                inception_val = v["time"]
                break
        # headquarters / industry: QID — 라벨 추출은 추가 호출이 필요해서 우선 QID 만
        hq_qids = claim_qid_values(entity, "P159")
        ind_qids = claim_qid_values(entity, "P452")
        # label_en — labels.en.value
        label_en = entity.get("labels", {}).get("en", {}).get("value")

        # facts 에도 보관 (qid 형태)
        for pid, qids in [("P159", hq_qids), ("P452", ind_qids), ("P169", claim_qid_values(entity, "P169"))]:
            for q in qids:
                fact_rows.append((
                    corp_code, qid, pid, q, "wikibase-item", q,
                    json.dumps({"property": pid, "value_qid": q}, ensure_ascii=False),
                ))

        neo4j_rows.append({
            "corp_code": corp_code,
            "qid": qid,
            "label_en": label_en,
            "inception": inception_val,
            "headquarters": hq_qids[0] if hq_qids else None,
            "industry": ind_qids[0] if ind_qids else None,
        })

    print(f"[load_wikidata] em_rows={len(em_rows)} fact_rows={len(fact_rows)} neo4j_rows={len(neo4j_rows)}")

    if args.dry_run:
        for r in em_rows[:5]:
            print("  EM:", r)
        for r in fact_rows[:5]:
            print("  F :", r[:6])
        return 0

    pool = get_pool()
    BATCH = 1000
    with pool.connection() as conn, conn.cursor() as cur:
        for i in range(0, len(em_rows), BATCH):
            cur.executemany(UPSERT_EM, em_rows[i:i + BATCH])
        for i in range(0, len(fact_rows), BATCH):
            cur.executemany(UPSERT_FACT, fact_rows[i:i + BATCH])

    # Neo4j 보강
    if not args.no_neo4j:
        from autonexusgraph.db.neo4j import get_driver
        with get_driver().session() as session:
            for i in range(0, len(neo4j_rows), 200):
                session.run(NEO4J_UPSERT, rows=neo4j_rows[i:i + 200])

    # 검증
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id_type, count(*) FROM master.entity_map
             WHERE source = 'wikidata'
             GROUP BY id_type ORDER BY 2 DESC
        """)
        print("\n[entity_map by id_type (wikidata source)]:")
        for r in cur.fetchall():
            print(f"  {r[0]:20s} {r[1]:>5}")
        cur.execute("SELECT count(*) FROM wiki.wikidata_facts")
        print(f"[wikidata_facts] total: {cur.fetchone()[0]:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
