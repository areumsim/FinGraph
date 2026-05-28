#!/usr/bin/env python3
"""P4 — P3 산출(processed/extracted/) cross-validate → Neo4j 적재.

처리 흐름:
1. data/processed/extracted/ 의 모든 jsonl 읽기
2. extractors.validator.validate_relations 로 accept / review / discard 분류
3. accept 만 Neo4j 에 적재 (PARTNER_OF / COMPETES_WITH / INVESTED_IN / PRODUCES)
4. review 는 별도 jsonl 큐 (data/reports/review_queue_<date>.jsonl) — 사람 검토용
5. discard 는 ops.quality_checks 에 사유 기록

LLM 호출 없음 — 비용 가드 불필요.
멱등: Cypher MERGE.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


CYPHER_RELATIONS = """
UNWIND $rows AS r
MATCH (head:Company {corp_code: r.head_corp})
MATCH (tail:Company {corp_code: r.tail_corp})
CALL apoc.merge.relationship(head, r.relation, {}, {
  source: 'p3_llm',
  extracted_at: datetime(),
  confidence: r.confidence,
  evidence: r.evidence,
  nature: r.nature,
  fiscal_year: r.fiscal_year,
  validated: true
}, tail, {}) YIELD rel
RETURN count(rel) AS n
"""

# APOC 없을 때 폴백 — relation type 별로 별도 cypher 4개.
CYPHER_BY_TYPE = {
    "PARTNER_OF": """
        UNWIND $rows AS r
        MATCH (a:Company {corp_code: r.head_corp})
        MATCH (b:Company {corp_code: r.tail_corp})
        MERGE (a)-[rel:PARTNER_OF]-(b)
        SET rel.source        = 'p3_llm',
            rel.extracted_at  = datetime(),
            rel.confidence    = r.confidence,
            rel.evidence      = r.evidence,
            rel.nature        = r.nature,
            rel.fiscal_year   = r.fiscal_year,
            rel.validated     = true
    """,
    "COMPETES_WITH": """
        UNWIND $rows AS r
        MATCH (a:Company {corp_code: r.head_corp})
        MATCH (b:Company {corp_code: r.tail_corp})
        MERGE (a)-[rel:COMPETES_WITH]-(b)
        SET rel.source       = 'p3_llm',
            rel.extracted_at = datetime(),
            rel.confidence   = r.confidence,
            rel.evidence     = r.evidence,
            rel.fiscal_year  = r.fiscal_year,
            rel.validated    = true
    """,
    "INVESTED_IN": """
        UNWIND $rows AS r
        MATCH (a:Company {corp_code: r.head_corp})
        MATCH (b:Company {corp_code: r.tail_corp})
        MERGE (a)-[rel:INVESTED_IN]->(b)
        SET rel.source        = 'p3_llm',
            rel.extracted_at  = datetime(),
            rel.confidence    = r.confidence,
            rel.evidence      = r.evidence,
            rel.ownership_pct = r.ownership_pct,
            rel.amount_krw    = r.amount_krw,
            rel.fiscal_year   = r.fiscal_year,
            rel.validated     = true
    """,
    "PRODUCES": """
        UNWIND $rows AS r
        MATCH (a:Company {corp_code: r.head_corp})
        MERGE (p:Product {name: r.tail, company_corp_code: r.head_corp})
        ON CREATE SET p.source = 'p3_llm', p.created_at = datetime()
        MERGE (a)-[rel:PRODUCES]->(p)
        SET rel.source       = 'p3_llm',
            rel.extracted_at = datetime(),
            rel.confidence   = r.confidence,
            rel.evidence     = r.evidence,
            rel.fiscal_year  = r.fiscal_year,
            rel.validated    = true
    """,
}


def _iter_processed(root: Path):
    """processed/extracted/<corp>/<rcept>.jsonl → P3 relation dicts."""
    if not root.exists():
        return
    for jl in root.rglob("*.jsonl"):
        for line in jl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            for rel in d.get("relations") or []:
                rel["_chunk_id"]    = d.get("chunk_id")
                rel["_corp_code"]   = d.get("corp_code")
                rel["_rcept_no"]    = d.get("rcept_no")
                rel["_fiscal_year"] = d.get("fiscal_year")
                yield rel


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--accept-conf", type=float, default=0.70)
    parser.add_argument("--review-conf", type=float, default=0.50)
    args = parser.parse_args()

    from autonexusgraph.config import get_settings
    from autonexusgraph.extractors.validator import validate_relations

    settings = get_settings()
    root = settings.ingest_processed_dir / "extracted"
    rels = list(_iter_processed(root))
    print(f"[P4] loaded P3 relations: {len(rels)}")
    if not rels:
        print("[P4] 처리할 P3 산출물 없음. extract_business_report_relations.py 먼저 실행.")
        return 0

    classified = validate_relations(rels,
                                    accept_threshold=args.accept_conf,
                                    review_threshold=args.review_conf)
    n_accept = len(classified["accept"])
    n_review = len(classified["review"])
    n_discard = len(classified["discard"])
    print(f"[P4] accept={n_accept} review={n_review} discard={n_discard}")

    # 1) review_queue.jsonl 저장
    if n_review:
        report_dir = Path("data/reports")
        report_dir.mkdir(parents=True, exist_ok=True)
        rev_path = report_dir / f"review_queue_{date.today().isoformat()}.jsonl"
        with rev_path.open("w", encoding="utf-8") as f:
            for v in classified["review"]:
                f.write(json.dumps({
                    "rel": v.rel, "reason": v.reason,
                    "confidence": v.final_confidence,
                }, ensure_ascii=False) + "\n")
        print(f"[P4] review queue: {rev_path}")

    # 2) discard → ops.quality_checks
    if n_discard:
        from autonexusgraph.db.postgres import get_pool
        with get_pool().connection() as conn, conn.cursor() as cur:
            for v in classified["discard"]:
                cur.execute("""
                    INSERT INTO ops.quality_checks
                      (check_name, target_id, severity, message, details)
                    VALUES ('p3_discarded', %s, 'warn', %s, %s)
                """, (
                    f"{v.rel.get('head','')}>{v.rel.get('relation','')}>{v.rel.get('tail','')}",
                    v.reason,
                    json.dumps(v.rel, ensure_ascii=False),
                ))

    if args.dry_run:
        print("[P4] --dry-run: Neo4j 적재 안 함")
        return 0

    if not n_accept:
        print("[P4] accept 0 — Neo4j 적재 skip.")
        return 0

    # 3) accept → Neo4j 적재 (relation type 별 batch)
    from autonexusgraph.db.neo4j import get_driver

    by_type: dict[str, list[dict]] = {}
    for v in classified["accept"]:
        rel = v.rel
        rtype = rel.get("relation")
        head_corp = rel.get("head_corp_code")
        tail_corp = rel.get("tail_corp_code")

        # PRODUCES 는 tail 이 Product (corp 없어도 OK)
        if rtype == "PRODUCES" and not head_corp:
            continue
        if rtype != "PRODUCES" and (not head_corp or not tail_corp):
            continue

        by_type.setdefault(rtype, []).append({
            "head_corp": head_corp,
            "tail_corp": tail_corp,
            "tail": rel.get("tail"),
            "relation": rtype,
            "confidence": v.final_confidence,
            "evidence": (rel.get("evidence") or "")[:300],
            "nature": rel.get("nature"),
            "ownership_pct": rel.get("ownership_pct"),
            "amount_krw": rel.get("amount_krw"),
            "fiscal_year": rel.get("_fiscal_year"),
        })

    with get_driver().session() as session:
        for rtype, rows in by_type.items():
            cypher = CYPHER_BY_TYPE.get(rtype)
            if not cypher:
                print(f"[P4] WARN unknown relation type: {rtype}, skip")
                continue
            BATCH = 200
            for i in range(0, len(rows), BATCH):
                session.run(cypher, rows=rows[i:i+BATCH])
            print(f"[P4] {rtype} adapted: {len(rows)}")

    print(f"[P4] Neo4j 적재 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
