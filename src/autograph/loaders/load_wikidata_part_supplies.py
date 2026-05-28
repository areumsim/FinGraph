"""data/raw/auto/wikidata/part_supplies.jsonl → auto.staging_relations.

Wikidata P176 (manufactured by) 매핑이 만든 (part, supplier) 쌍을 staging 에
SUPPLIED_BY 후보로 적재. PRD §3.5: Wikidata = B 등급 = 기본 confidence 0.80
+ deterministic 추출이라 gate_status='auto_accept' 후보.

이미 등록된 staging row (merge key 동일) 는 confidence 가 더 높을 때만 갱신
(staging_writer.upsert_staging 의 ON CONFLICT 와 동일 정책).

이후 ``python -m autograph.extractors.cross_validate`` 가 staging 을 P4 로 처리해
Neo4j 의 (:Module|:Part)-[:SUPPLIED_BY]->(:Supplier) 엣지로 promote.

CLI:
    python -m autograph.loaders.load_wikidata_part_supplies
    python -m autograph.loaders.load_wikidata_part_supplies --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name


log = logging.getLogger(__name__)


# PRD §3.5: Wikidata deterministic = B 등급 → 0.80. P176 매칭은 명시적이므로
# auto_accept 게이트.
_WIKIDATA_PART_CONFIDENCE = 0.80
_EXTRACTOR_NAME = "wikidata_p176"
_EXTRACTOR_VERSION = "v1"


@dataclass
class LoadStats:
    rows_seen:     int = 0
    rows_inserted: int = 0
    rows_updated:  int = 0
    rows_skipped:  int = 0
    errors: list[str] = field(default_factory=list)


def _wikidata_root() -> Path:
    return get_settings().ingest_raw_dir / "auto" / "wikidata"


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("[load:wd_p176] bad json in %s: %s", path, e)
                continue


def load_part_supplies(*, dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    src = _wikidata_root() / "part_supplies.jsonl"
    if not src.exists():
        log.warning("[load:wd_p176] %s 없음 — ingest_kind('part_supplies') 선행 필요",
                    src)
        return stats

    conn = get_connection()
    with conn.cursor() as cur:
        for row in _iter_jsonl(src):
            stats.rows_seen += 1
            part_qid = row.get("part_qid")
            part_name = row.get("partLabel")
            supplier_qid = row.get("supplier_qid")
            supplier_name = row.get("supplierLabel")

            if not (part_qid and part_name and supplier_qid and supplier_name):
                stats.rows_skipped += 1
                continue

            # part / supplier 가 QID 형태로 들어왔는지 sanity check.
            # 라벨이 QID 그대로 ('Q12345') 인 경우는 Wikidata label 부재 → skip.
            if part_name.startswith("Q") and part_name[1:].isdigit():
                stats.rows_skipped += 1
                continue
            if supplier_name.startswith("Q") and supplier_name[1:].isdigit():
                stats.rows_skipped += 1
                continue

            head_text = part_name.strip()
            tail_text = supplier_name.strip()
            head_norm = normalize_corp_name(head_text)
            tail_norm = normalize_corp_name(tail_text)

            evidence = (
                f"Wikidata: {head_text} ({part_qid}) "
                f"-[P176 manufactured by]-> {tail_text} ({supplier_qid})"
            )

            cur.execute("SAVEPOINT sp_wd_p176")
            try:
                # ON CONFLICT (merge key) → 더 높은 confidence 로만 update.
                cur.execute("""
                    INSERT INTO auto.staging_relations
                      (relation_type, head_kind, head_text_norm, tail_kind, tail_text_norm,
                       snapshot_year, head_pg_id, tail_pg_id,
                       head_text, tail_text,
                       confidence_score, evidence_text, evidence_chunk_ids,
                       extractor_name, extractor_version, gate_status, raw)
                    VALUES (%s, %s, %s, %s, %s, NULL,
                            NULL, NULL,
                            %s, %s,
                            %s, %s, ARRAY[]::bigint[],
                            %s, %s, %s, %s::jsonb)
                    ON CONFLICT (relation_type, head_kind, head_text_norm,
                                 tail_kind, tail_text_norm,
                                 COALESCE(snapshot_year, 0))
                    DO UPDATE SET
                      confidence_score = GREATEST(
                          auto.staging_relations.confidence_score,
                          EXCLUDED.confidence_score),
                      evidence_text = CASE
                          WHEN EXCLUDED.confidence_score
                               > auto.staging_relations.confidence_score
                          THEN EXCLUDED.evidence_text
                          ELSE auto.staging_relations.evidence_text END,
                      gate_status = CASE
                          WHEN EXCLUDED.confidence_score
                               > auto.staging_relations.confidence_score
                          THEN EXCLUDED.gate_status
                          ELSE auto.staging_relations.gate_status END
                    RETURNING (xmax = 0) AS inserted
                """, (
                    "SUPPLIED_BY", "Module", head_norm, "Supplier", tail_norm,
                    head_text, tail_text,
                    _WIKIDATA_PART_CONFIDENCE,
                    evidence,
                    _EXTRACTOR_NAME, _EXTRACTOR_VERSION,
                    "auto_accept",  # 0.80 ≥ 0.80 임계
                    json.dumps({
                        "source":       "wikidata_p176",
                        "part_qid":     part_qid,
                        "supplier_qid": supplier_qid,
                        "country":      row.get("countryLabel"),
                    }, ensure_ascii=False),
                ))
                inserted = cur.fetchone()[0]
                cur.execute("RELEASE SAVEPOINT sp_wd_p176")
                if inserted:
                    stats.rows_inserted += 1
                else:
                    stats.rows_updated += 1
            except Exception as e:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_wd_p176")
                stats.errors.append(f"{part_qid}->{supplier_qid}: {e}")

    if dry_run:
        conn.rollback()
        log.info("[load:wd_p176] DRY-RUN seen=%d would_insert=%d would_update=%d",
                 stats.rows_seen, stats.rows_inserted, stats.rows_updated)
    else:
        conn.commit()
        log.info("[load:wd_p176] seen=%d inserted=%d updated=%d skipped=%d errors=%d",
                 stats.rows_seen, stats.rows_inserted, stats.rows_updated,
                 stats.rows_skipped, len(stats.errors))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_wikidata_part_supplies")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_part_supplies(dry_run=args.dry_run)


if __name__ == "__main__":
    main()


__all__ = ["load_part_supplies", "LoadStats"]
