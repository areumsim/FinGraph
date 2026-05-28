#!/usr/bin/env python3
"""Neo4j 그래프 스키마 정합성 마이그레이션 (멱등).

대상:
1. Sector / IN_SECTOR  ──→  Industry / IN_INDUSTRY 로 라벨·관계명 통일
   (ontology/relations.yaml 의 IN_INDUSTRY 와 일치시킴)
2. Person.birth_year 누락 노드에 -1 부여
   (graph_structural.py 의 새 MERGE 키 (name, birth_year) 와 정합)
3. 관계 source / extracted_at 속성 백필 (provenance 추적용)

여러 번 실행해도 결과 동일. 매 작업마다 변경 카운트 출력.

사용:
    python scripts/migrate_neo4j_schema.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.db.neo4j import get_driver


MIGRATIONS = [
    # 1. Sector 노드 라벨 → Industry  (Sector 만 단독 라벨일 때만)
    (
        "label Sector → Industry",
        """
        MATCH (s:Sector)
        WHERE NOT s:Industry
        SET s:Industry
        REMOVE s:Sector
        RETURN count(s) AS n
        """,
    ),
    # 2. 잔존 Sector 라벨 + IN_SECTOR 관계가 둘 다 있을 때 → IN_INDUSTRY 로 복제 후 제거
    (
        "rel IN_SECTOR → IN_INDUSTRY",
        """
        MATCH (c:Company)-[r:IN_SECTOR]->(s)
        MERGE (c)-[r2:IN_INDUSTRY]->(s)
        ON CREATE SET r2.source = coalesce(r.source, 'dart')
        DELETE r
        RETURN count(r2) AS n
        """,
    ),
    # 3. Person.birth_year NULL → -1 부여 (graph_structural 의 새 키 (name, birth_year) 와 정합)
    (
        "Person.birth_year NULL → -1",
        """
        MATCH (p:Person)
        WHERE p.birth_year IS NULL
        SET p.birth_year = -1
        RETURN count(p) AS n
        """,
    ),
    # 4. SUBSIDIARY_OF / RELATED_TO 관계 source 백필
    (
        "SUBSIDIARY_OF backfill source",
        """
        MATCH ()-[r:SUBSIDIARY_OF]->()
        WHERE r.source IS NULL
        SET r.source = 'dart_otr_cpr_invstmnt'
        RETURN count(r) AS n
        """,
    ),
    (
        "RELATED_TO backfill source",
        """
        MATCH ()-[r:RELATED_TO]->()
        WHERE r.source IS NULL
        SET r.source = 'dart_otr_cpr_invstmnt'
        RETURN count(r) AS n
        """,
    ),
    (
        "EXECUTIVE_OF backfill source",
        """
        MATCH ()-[r:EXECUTIVE_OF]->()
        WHERE r.source IS NULL
        SET r.source = 'dart_exctv_sttus'
        RETURN count(r) AS n
        """,
    ),
    (
        "MAJOR_SHAREHOLDER_OF backfill source",
        """
        MATCH ()-[r:MAJOR_SHAREHOLDER_OF]->()
        WHERE r.source IS NULL
        SET r.source = 'dart_hyslr_sttus'
        RETURN count(r) AS n
        """,
    ),
    # 5. 인덱스 idempotent — (name, birth_year) 복합
    (
        "index person_name_birth",
        "CREATE INDEX person_name_birth IF NOT EXISTS FOR (p:Person) ON (p.name, p.birth_year)",
    ),
    (
        "index industry_code",
        "CREATE INDEX industry_code IF NOT EXISTS FOR (i:Industry) ON (i.code)",
    ),
    (
        "index newsevent_hash",
        "CREATE INDEX newsevent_hash IF NOT EXISTS FOR (n:NewsEvent) ON (n.article_hash)",
    ),
    (
        "index group_name",
        "CREATE INDEX group_name IF NOT EXISTS FOR (g:Group) ON (g.name)",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="실행 안 함, 마이그레이션 목록만 표시")
    args = parser.parse_args()

    if args.dry_run:
        print("[migrate] dry-run — 다음 마이그레이션이 실행될 예정:")
        for name, _ in MIGRATIONS:
            print(f"  - {name}")
        return 0

    driver = get_driver()
    with driver.session() as session:
        for name, cypher in MIGRATIONS:
            try:
                result = session.run(cypher)
                row = result.single()
                affected = row["n"] if row and "n" in row.keys() else "—"
                print(f"[migrate] {name:45s} affected={affected}")
            except Exception as e:
                print(f"[migrate] {name:45s} FAIL: {e}", file=sys.stderr)
    print("\n[migrate] 완료. 변경 카운트가 0 이면 이미 마이그레이션 완료된 상태.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
