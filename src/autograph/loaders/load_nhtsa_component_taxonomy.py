"""auto.events_recalls.component_text → auto.components 직접 등록.

NHTSA 표준 component_text 는 ``SYSTEM:SUBSYSTEM:PART`` 형식 (예:
``ELECTRICAL SYSTEM:INSTRUMENT CLUSTER/PANEL``). 본 loader 는 이를:

    - auto.components 에 level=4 module 로 등록 (source='nhtsa_recall_taxonomy')
    - 첫 ``:`` 앞을 system_code 로 정규화 (예: ``electrical_system``)
    - events_recalls.component_id 를 새로 매핑된 component_id 로 backfill

매핑 결과는 load_recall_components 또는 load_auto_neo4j 재실행 시 RECALL_OF
edge 로 자동 반영 → BOM Level 4 coverage 자동 향상.

PRD §10.5 직접 충족용. token-매칭 (load_recall_components) 보다 정확.

CLI:
    python -m autograph.loaders.load_nhtsa_component_taxonomy
    python -m autograph.loaders.load_nhtsa_component_taxonomy --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from ._text_utils import norm_text as _norm


log = logging.getLogger(__name__)


_SYS_CODE_RE = re.compile(r"[^\w]+", re.UNICODE)


def _system_code(text: str) -> str:
    """component_text 의 첫 ':' 앞을 system_code 로 정규화."""
    head = (text or "").split(":", 1)[0].strip()
    return _SYS_CODE_RE.sub("_", head.lower()).strip("_")[:64]


@dataclass
class LoadStats:
    distinct_categories: int = 0
    components_inserted: int = 0
    components_existing: int = 0
    recalls_backfilled:  int = 0
    errors: list[str]    = field(default_factory=list)


def _iter_categories(cur) -> Iterable[tuple[str, int]]:
    cur.execute("""
        SELECT component_text, COUNT(*) AS n FROM auto.events_recalls
         WHERE component_text IS NOT NULL AND component_text != ''
         GROUP BY component_text
         ORDER BY n DESC
    """)
    for r in cur.fetchall():
        yield str(r[0]), int(r[1])


def _ensure_component(cur, name: str, *, snapshot_year: int) -> tuple[int, bool]:
    """등록 또는 기존 조회. (component_id, inserted) 반환."""
    norm = _norm(name)
    cur.execute("""
        SELECT component_id FROM auto.components
         WHERE name_norm = %s LIMIT 1
    """, (norm,))
    r = cur.fetchone()
    if r:
        return int(r[0]), False

    cur.execute("SELECT GREATEST(COALESCE(MAX(component_id), 0), 1000) + 1 FROM auto.components")
    new_id = int(cur.fetchone()[0])
    sys_code = _system_code(name)
    cur.execute("""
        INSERT INTO auto.components
            (component_id, canonical_name, name_norm, system_code,
             aliases, wikidata_qid, source, confidence,
             validated_status, notes, level, parent_component_id,
             snapshot_year)
        VALUES (%s, %s, %s, %s, %s, NULL, 'nhtsa_recall_taxonomy', 0.95,
                'reviewed', '', 4, NULL, %s)
    """, (new_id, name, norm, sys_code, [], snapshot_year))
    return new_id, True


def load_component_taxonomy(*, dry_run: bool = False) -> LoadStats:
    import autonexusgraph.db.postgres as pg
    stats = LoadStats()
    conn = pg.get_connection()

    import datetime as _dt
    cur_year = _dt.datetime.now(tz=_dt.timezone.utc).year

    with conn.cursor() as cur:
        # 1) NHTSA 표준 카테고리 → auto.components.
        cat_to_id: dict[str, int] = {}
        for name, _n in _iter_categories(cur):
            stats.distinct_categories += 1
            try:
                cid, inserted = _ensure_component(cur, name,
                                                    snapshot_year=cur_year)
                cat_to_id[name] = cid
                if inserted:
                    stats.components_inserted += 1
                else:
                    stats.components_existing += 1
            except Exception as e:   # noqa: BLE001
                stats.errors.append(f"comp[{name}]: {e}")

        # 2) events_recalls.component_id 를 backfill (현재 NULL 인 행만).
        # 한 번에 UPDATE — staging table 으로 join.
        if cat_to_id:
            rows = [(cid, name) for name, cid in cat_to_id.items()]
            cur.execute("""
                CREATE TEMP TABLE _tmp_comp_map (
                    component_id BIGINT, component_text TEXT
                ) ON COMMIT DROP
            """)
            cur.executemany(
                "INSERT INTO _tmp_comp_map (component_id, component_text) "
                "VALUES (%s, %s)",
                rows,
            )
            cur.execute("""
                UPDATE auto.events_recalls r
                   SET component_id = t.component_id
                  FROM _tmp_comp_map t
                 WHERE r.component_text = t.component_text
                   AND r.component_id IS NULL
            """)
            stats.recalls_backfilled = cur.rowcount

    if dry_run:
        conn.rollback()
        log.info(
            "[load:nhtsa_taxonomy] DRY-RUN cats=%d ins=%d exist=%d "
            "backfilled=%d errors=%d",
            stats.distinct_categories, stats.components_inserted,
            stats.components_existing, stats.recalls_backfilled,
            len(stats.errors),
        )
        return stats

    conn.commit()
    log.info(
        "[load:nhtsa_taxonomy] cats=%d ins=%d exist=%d backfilled=%d errors=%d",
        stats.distinct_categories, stats.components_inserted,
        stats.components_existing, stats.recalls_backfilled,
        len(stats.errors),
    )
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_nhtsa_component_taxonomy")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_component_taxonomy(dry_run=args.dry_run)


if __name__ == "__main__":
    main()


__all__ = [
    "load_component_taxonomy", "LoadStats",
    "_norm", "_system_code", "_ensure_component", "_iter_categories",
]
