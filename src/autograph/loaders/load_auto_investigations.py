"""data/raw/auto/nhtsa_investigations/FLAT_INV.zip → auto.events_investigations + Neo4j.

PRD §3.5: NHTSA ODI = A 등급 (0.95). 리콜 전단계 — 잠재적 결함 시계열.

PG 적재:
    FLAT_INV TAB-delimited (11 컬럼, no header) → auto.events_investigations.
    멱등: UNIQUE(source, action_number) ON CONFLICT DO UPDATE.

Neo4j 적재:
    1) (:Investigation {id}) 노드 MERGE
    2) variant 매칭이 있으면 (:VehicleVariant)-[:INVESTIGATED_BY]->(:Investigation)
       없고 model 매칭이 있으면 (:VehicleModel)-[:INVESTIGATED_BY]->(:Investigation)
    3) campno 가 채워졌고 해당 리콜이 그래프에 있으면
       (:Investigation)-[:LED_TO_RECALL]->(:Recall) 부가 엣지 (조사→리콜 종결).

NHTSA_ACTION_NUMBER 의 첫 2글자가 investigation_type:
    PE  Preliminary Evaluation
    EA  Engineering Analysis
    RQ  Recall Query
    AQ  Audit Query
    DP  Defect Petition

CLI:
    python -m autograph.loaders.load_auto_investigations
    python -m autograph.loaders.load_auto_investigations --dry-run
    python -m autograph.loaders.load_auto_investigations --batch 500
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from autonexusgraph.config import get_settings
from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name

from ._neo4j_helpers import run_batched


log = logging.getLogger(__name__)


_SOURCE_KEY = "nhtsa_odi"
_CONFIDENCE = 0.95
_INV_TYPE_PREFIXES = {"PE", "EA", "RQ", "AQ", "DP"}

# FLAT_INV 의 컬럼 순서 (no header). INV.txt 1~11.
_COLUMNS = (
    "NHTSA_ACTION_NUMBER", "MAKE", "MODEL", "YEAR", "COMPNAME",
    "MFR_NAME", "ODATE", "CDATE", "CAMPNO", "SUBJECT", "SUMMARY",
)


@dataclass
class LoadStats:
    rows_seen:           int = 0
    rows_unmatched:      int = 0   # variant/model 매칭 0 — 노드는 만들고 엣지 없음
    rows_inserted:       int = 0
    rows_updated:        int = 0
    rows_errors:         int = 0
    nodes_merged:        int = 0
    edges_variant:       int = 0
    edges_model:         int = 0
    edges_led_to_recall: int = 0
    errors: list[str] = field(default_factory=list)


def _inv_root() -> Path:
    return get_settings().ingest_raw_dir / "auto" / "nhtsa_investigations"


def _iter_inv_rows(zip_path: Path) -> Iterator[dict[str, str]]:
    """FLAT_INV.zip 내부의 TAB-delimited txt → dict 순회. 헤더 없으므로 _COLUMNS 강제."""
    if not zip_path.exists():
        return
    with zipfile.ZipFile(zip_path) as z:
        txt_names = [n for n in z.namelist() if n.upper().endswith(".TXT")]
        if not txt_names:
            raise FileNotFoundError(f"zip 안 txt 없음: {zip_path}")
        with z.open(txt_names[0]) as f:
            wrapper = io.TextIOWrapper(
                f, encoding="utf-8-sig", errors="replace", newline="",
            )
            reader = csv.reader(wrapper, delimiter="\t", quoting=csv.QUOTE_NONE)
            for row in reader:
                if not row:
                    continue
                # 짧은 row (마지막 줄 끊김) 도 dict 으로 — 부족분은 빈 문자열.
                values = list(row) + [""] * (len(_COLUMNS) - len(row))
                yield dict(zip(_COLUMNS, values[: len(_COLUMNS)]))


def _parse_date(s: str | None) -> str | None:
    """YYYYMMDD → 'YYYY-MM-DD'. 빈/잘못된 값 → None."""
    if not s:
        return None
    s = str(s).strip()
    if len(s) != 8 or not s.isdigit():
        return None
    yyyy, mm, dd = s[:4], s[4:6], s[6:8]
    if mm == "00" or dd == "00":
        return None
    return f"{yyyy}-{mm}-{dd}"


def _parse_year(s: str | None) -> int | None:
    if not s:
        return None
    s = str(s).strip()
    if not s.isdigit():
        return None
    y = int(s)
    if y == 9999 or y < 1900 or y > 2099:
        return None
    return y


def _investigation_type(action_no: str | None) -> str | None:
    """'PE12001' → 'PE'. 알 수 없으면 None."""
    if not action_no:
        return None
    s = str(action_no).strip().upper()
    if len(s) < 2:
        return None
    pref = s[:2]
    return pref if pref in _INV_TYPE_PREFIXES else None


def _resolve_targets(cur, *, make: str, model: str, year: int | None
                     ) -> tuple[int | None, int | None, int | None]:
    """(make, model, year) → (manufacturer_id, model_id, variant_id) 단일 LEFT JOIN.

    load_auto_pg._resolve_make_model_variant 패턴 재사용 — model_year NULL 도 OK.
    """
    if not make:
        return None, None, None
    cur.execute("""
        SELECT mm.manufacturer_id, m.model_id, v.variant_id
          FROM auto.master_manufacturers mm
          LEFT JOIN auto.master_vehicle_models m
            ON m.manufacturer_id = mm.manufacturer_id
           AND m.name_norm = %s
          LEFT JOIN auto.master_vehicle_variants v
            ON v.model_id = m.model_id
           AND v.model_year = %s::int
         WHERE mm.name_norm = %s
         LIMIT 1
    """, (
        normalize_corp_name(model) if model else None,
        year,
        normalize_corp_name(make),
    ))
    row = cur.fetchone()
    if not row:
        return None, None, None
    return row[0], row[1], row[2]


# ── Neo4j MERGE Cypher ─────────────────────────────────────
_MERGE_INVESTIGATION = """
UNWIND $rows AS r
MERGE (inv:Investigation {id: r.id})
SET   inv.source              = r.source,
      inv.action_number       = r.action_number,
      inv.investigation_type  = r.investigation_type,
      inv.opened_date         = r.opened_date,
      inv.closed_date         = r.closed_date,
      inv.subject             = r.subject,
      inv.summary             = r.summary,
      inv.country             = r.country,
      inv.mfr_name            = r.mfr_name,
      inv.snapshot_year       = r.snapshot_year,
      inv.campno              = r.campno,
      inv.updated_at          = datetime()
"""

_MERGE_VARIANT_EDGE = """
UNWIND $rows AS r
MATCH (inv:Investigation {id: r.id})
WITH inv, r WHERE r.variant_id IS NOT NULL
OPTIONAL MATCH (v:VehicleVariant {id: r.variant_id})
WITH inv, r, v WHERE v IS NOT NULL
MERGE (v)-[rel:INVESTIGATED_BY]->(inv)
SET   rel.source_id        = r.action_number,
      rel.source_type      = 'pg.auto.events_investigations',
      rel.extraction_method = 'deterministic',
      rel.confidence_score = r.confidence,
      rel.validated_status = 'verified',
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year)
"""

_MERGE_MODEL_EDGE = """
UNWIND $rows AS r
MATCH (inv:Investigation {id: r.id})
WITH inv, r WHERE r.variant_id IS NULL AND r.model_id IS NOT NULL
OPTIONAL MATCH (m:VehicleModel {id: r.model_id})
WITH inv, r, m WHERE m IS NOT NULL
MERGE (m)-[rel:INVESTIGATED_BY]->(inv)
SET   rel.source_id        = r.action_number,
      rel.source_type      = 'pg.auto.events_investigations',
      rel.extraction_method = 'deterministic',
      rel.confidence_score = r.confidence,
      rel.validated_status = 'verified',
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year)
"""

# 조사가 리콜로 종결됐다는 신호 — campno 가 채워진 row 만.
_MERGE_LED_TO_RECALL = """
UNWIND $rows AS r
MATCH (inv:Investigation {id: r.id})
WITH inv, r WHERE r.campno IS NOT NULL
OPTIONAL MATCH (rc:Recall)
  WHERE rc.source_recall_no = r.campno
WITH inv, r, rc WHERE rc IS NOT NULL
MERGE (inv)-[rel:LED_TO_RECALL]->(rc)
SET   rel.source_type      = 'pg.auto.events_investigations',
      rel.source_id        = r.action_number,
      rel.extraction_method = 'deterministic',
      rel.confidence_score = r.confidence,
      rel.validated_status = 'verified',
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year)
"""


def _upsert_pg(cur, row: dict[str, str], stats: LoadStats) -> tuple[bool, dict] | None:
    """단일 row 를 auto.events_investigations 에 UPSERT.

    Returns:
        (inserted_bool, neo4j_payload) — neo4j_payload 는 Neo4j 적재용 dict.
        skip 이면 None.
    """
    action_no = (row.get("NHTSA_ACTION_NUMBER") or "").strip()
    if not action_no:
        return None

    make = (row.get("MAKE") or "").strip()
    model = (row.get("MODEL") or "").strip()
    year = _parse_year(row.get("YEAR"))
    inv_type = _investigation_type(action_no)

    manufacturer_id, model_id, variant_id = _resolve_targets(
        cur, make=make, model=model, year=year,
    )

    odate = _parse_date(row.get("ODATE"))
    cdate = _parse_date(row.get("CDATE"))
    campno = (row.get("CAMPNO") or "").strip() or None
    subject = (row.get("SUBJECT") or "").strip() or None
    summary = (row.get("SUMMARY") or "").strip() or None
    component_text = (row.get("COMPNAME") or "").strip() or None
    mfr_name = (row.get("MFR_NAME") or "").strip() or None

    # snapshot_year = 조사 개시 연도 (없으면 year, 그것도 없으면 적재 연도).
    snap_year = None
    if odate:
        try:
            snap_year = int(odate[:4])
        except (TypeError, ValueError):
            snap_year = None

    cur.execute("""
        INSERT INTO auto.events_investigations
          (source, action_number, investigation_type,
           manufacturer_id, model_id, variant_id,
           mfr_name, component_text,
           opened_date, closed_date, campno,
           subject, summary, country,
           snapshot_year, raw)
        VALUES (%s, %s, %s,
                %s, %s, %s,
                %s, %s,
                NULLIF(%s, '')::date, NULLIF(%s, '')::date, %s,
                %s, %s, 'US',
                COALESCE(%s, EXTRACT(YEAR FROM now())::SMALLINT), %s::jsonb)
        ON CONFLICT (source, action_number) DO UPDATE SET
          investigation_type = COALESCE(EXCLUDED.investigation_type,
                                         auto.events_investigations.investigation_type),
          manufacturer_id    = COALESCE(EXCLUDED.manufacturer_id,
                                         auto.events_investigations.manufacturer_id),
          model_id           = COALESCE(EXCLUDED.model_id,
                                         auto.events_investigations.model_id),
          variant_id         = COALESCE(EXCLUDED.variant_id,
                                         auto.events_investigations.variant_id),
          subject            = EXCLUDED.subject,
          summary            = EXCLUDED.summary,
          closed_date        = EXCLUDED.closed_date,
          campno             = EXCLUDED.campno,
          raw                = EXCLUDED.raw,
          ingested_at        = now()
        RETURNING investigation_id, (xmax = 0) AS inserted
    """, (
        _SOURCE_KEY, action_no, inv_type,
        manufacturer_id, model_id, variant_id,
        mfr_name, component_text,
        odate or "", cdate or "", campno,
        subject, summary,
        snap_year,
        json.dumps({
            "action_number": action_no,
            "make": make, "model": model, "year_raw": row.get("YEAR"),
            "component_text": component_text,
        }, ensure_ascii=False),
    ))
    rec = cur.fetchone()
    if not rec:
        return None
    investigation_id, inserted = rec

    if variant_id is None and model_id is None:
        stats.rows_unmatched += 1

    payload = {
        "id": int(investigation_id),
        "source": _SOURCE_KEY,
        "action_number": action_no,
        "investigation_type": inv_type,
        "opened_date": odate,
        "closed_date": cdate,
        "subject": subject,
        "summary": (summary or "")[:1000],   # Neo4j 노드 속성 길이 절제
        "country": "US",
        "mfr_name": mfr_name,
        "snapshot_year": snap_year,
        "campno": campno,
        "manufacturer_id": manufacturer_id,
        "model_id": model_id,
        "variant_id": variant_id,
        "confidence": _CONFIDENCE,
    }
    return inserted, payload


def load_investigations(*, dry_run: bool = False, batch: int = 500) -> LoadStats:
    stats = LoadStats()
    zip_path = _inv_root() / "FLAT_INV.zip"
    if not zip_path.exists():
        log.warning("[load:inv] FLAT_INV.zip 없음 — ingestion 먼저 실행: %s", zip_path)
        return stats

    conn = get_connection()
    payloads: list[dict] = []

    with conn.cursor() as cur:
        for row in _iter_inv_rows(zip_path):
            stats.rows_seen += 1
            cur.execute("SAVEPOINT sp_inv")
            try:
                out = _upsert_pg(cur, row, stats)
                if out is None:
                    cur.execute("RELEASE SAVEPOINT sp_inv")
                    continue
                inserted, payload = out
                cur.execute("RELEASE SAVEPOINT sp_inv")
                if inserted:
                    stats.rows_inserted += 1
                else:
                    stats.rows_updated += 1
                payloads.append(payload)
            except Exception as e:   # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_inv")
                stats.rows_errors += 1
                if len(stats.errors) < 20:    # 너무 많이 누적 안 함
                    stats.errors.append(
                        f"{row.get('NHTSA_ACTION_NUMBER','?')}: {e}"
                    )

    if dry_run:
        conn.rollback()
        log.info("[load:inv] DRY-RUN seen=%d ins=%d upd=%d unmatched=%d errors=%d",
                 stats.rows_seen, stats.rows_inserted, stats.rows_updated,
                 stats.rows_unmatched, stats.rows_errors)
        return stats

    conn.commit()

    if payloads:
        driver = get_driver()
        with driver.session() as session:
            stats.nodes_merged = run_batched(
                session, _MERGE_INVESTIGATION, payloads, batch=batch,
            )
            stats.edges_variant = run_batched(
                session, _MERGE_VARIANT_EDGE, payloads, batch=batch,
            )
            stats.edges_model = run_batched(
                session, _MERGE_MODEL_EDGE, payloads, batch=batch,
            )
            stats.edges_led_to_recall = run_batched(
                session, _MERGE_LED_TO_RECALL, payloads, batch=batch,
            )

    log.info(
        "[load:inv] seen=%d ins=%d upd=%d unmatched=%d errors=%d "
        "neo4j: nodes=%d variant_edges=%d model_edges=%d led_to_recall=%d",
        stats.rows_seen, stats.rows_inserted, stats.rows_updated,
        stats.rows_unmatched, stats.rows_errors,
        stats.nodes_merged, stats.edges_variant,
        stats.edges_model, stats.edges_led_to_recall,
    )
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_investigations")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_investigations(dry_run=args.dry_run, batch=args.batch)


if __name__ == "__main__":
    main()


__all__ = [
    "load_investigations", "LoadStats",
    "_iter_inv_rows", "_parse_date", "_parse_year", "_investigation_type",
    "_resolve_targets", "_upsert_pg", "_COLUMNS",
    "_MERGE_INVESTIGATION", "_MERGE_VARIANT_EDGE", "_MERGE_MODEL_EDGE",
    "_MERGE_LED_TO_RECALL",
    "_SOURCE_KEY", "_CONFIDENCE", "_INV_TYPE_PREFIXES",
]
