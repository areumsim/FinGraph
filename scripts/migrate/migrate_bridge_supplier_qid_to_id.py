#!/usr/bin/env python3
"""bridge.corp_entity 의 supplier row 마이그레이션 — entity_id Q*→numeric.

배경:
    초기 load_bridge 가 supplier 의 entity_id 에 Wikidata QID 를 그대로
    저장했다 (예 'Q246', 'Q1346' 등). 이후 코드는 stringified supplier_id
    컨벤션 (`auto.master_suppliers.supplier_id`) 으로 변경됐지만 옛 row
    4,000+ 가 잔존. 결과:
    1) `eval/metrics/bridge_quality.py` 의 supplier_id IN (SELECT entity_id::bigint ...)
       cast 가 실패 → query fail
    2) Neo4j 의 :Supplier 노드와 entity_id 매칭 불일치 → SUPPLIED_BY 적재 실패

본 스크립트 동작 (멱등):
    1) bridge.corp_entity WHERE entity_type='supplier' AND entity_id ~ '^Q...'
       전체 fetch.
    2) 각 row 의 wikidata_qid → auto.master_suppliers UPSERT
       (load_bridge._ensure_supplier 와 동일 패턴) → 새 supplier_id 발급.
    3) bridge.corp_entity 업데이트: entity_id ← str(supplier_id),
       기존 entity_id_legacy 컬럼은 만들지 않음 (wikidata_qid 컬럼에 이미 보존).

CLI:
    python -m scripts.migrate.migrate_bridge_supplier_qid_to_id
    python -m scripts.migrate.migrate_bridge_supplier_qid_to_id --dry-run
    python -m scripts.migrate.migrate_bridge_supplier_qid_to_id --limit 100
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# repo src 를 path 에 추가 (scripts 실행 시).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from autonexusgraph.db.postgres import get_connection
from autograph.loaders.load_bridge import _ensure_supplier


log = logging.getLogger(__name__)


def migrate(*, dry_run: bool = False, limit: int | None = None) -> dict:
    """QID-only supplier bridge row → stringified supplier_id 마이그.

    Returns: stats dict.
    """
    stats = {"seen": 0, "migrated": 0, "skipped_no_qid": 0,
             "errors": 0}
    conn = get_connection()
    with conn.cursor() as cur:
        # 1) QID-only supplier row fetch.
        sql = """
            SELECT entity_id, name, wikidata_qid
              FROM bridge.corp_entity
             WHERE entity_type = 'supplier'
               AND entity_id ~ '^Q[0-9]+$'
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur.execute(sql)
        rows = cur.fetchall()
        log.info("[migrate] %d QID-only supplier rows 발견", len(rows))

        for entity_id_old, name, qid in rows:
            stats["seen"] += 1
            # wikidata_qid 컬럼이 비어있거나 entity_id 와 다르면 entity_id 가 권위.
            effective_qid = qid or entity_id_old
            if not effective_qid:
                stats["skipped_no_qid"] += 1
                continue
            try:
                # auto.master_suppliers UPSERT — supplier_id 발급/조회.
                supplier_id = _ensure_supplier(
                    cur,
                    name=name or effective_qid,
                    wikidata_qid=effective_qid,
                    country=None, lei=None, business_no=None,
                    source="migrate_bridge_supplier_qid",
                    source_ref=effective_qid,
                    confidence=0.55,    # legacy candidate 의 기본값 유지
                )
                new_entity_id = str(supplier_id)

                # bridge.corp_entity 의 entity_id 업데이트.
                # UNIQUE(corp_code, entity_type, entity_id) 제약 충돌 회피:
                # 같은 supplier_id 의 numeric entity_id row 가 이미 있으면 옛 row 삭제.
                cur.execute("""
                    SELECT 1 FROM bridge.corp_entity
                     WHERE entity_type='supplier'
                       AND entity_id=%s
                       AND COALESCE(corp_code,'')=COALESCE(
                             (SELECT corp_code FROM bridge.corp_entity
                               WHERE entity_type='supplier' AND entity_id=%s LIMIT 1),
                             '')
                     LIMIT 1
                """, (new_entity_id, entity_id_old))
                if cur.fetchone():
                    # 새 numeric row 가 이미 있음 → 옛 QID row 만 삭제.
                    cur.execute("""
                        DELETE FROM bridge.corp_entity
                         WHERE entity_type='supplier' AND entity_id=%s
                    """, (entity_id_old,))
                else:
                    # entity_id 만 in-place 갱신.
                    cur.execute("""
                        UPDATE bridge.corp_entity
                           SET entity_id  = %s,
                               updated_at = now()
                         WHERE entity_type='supplier' AND entity_id=%s
                    """, (new_entity_id, entity_id_old))
                stats["migrated"] += 1
            except Exception as e:   # noqa: BLE001
                stats["errors"] += 1
                log.warning("[migrate] %s 실패: %s", entity_id_old, e)
                conn.rollback()
                continue

    if dry_run:
        conn.rollback()
        log.info("[migrate] DRY-RUN — 롤백. stats=%s", stats)
    else:
        conn.commit()
        log.info("[migrate] commit. stats=%s", stats)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="migrate_bridge_supplier_qid_to_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="N개 row 만 마이그 (smoke 용)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    migrate(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
