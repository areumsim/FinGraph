"""P3 산출 → auto.staging_relations UPSERT.

ExtractorEngine 의 merged_relations 출력 (dict 리스트) 을 받아 PG staging 테이블에 적재.
merge key 는 (relation_type, head_kind, head_text_norm, tail_kind, tail_text_norm,
COALESCE(snapshot_year, 0)) — 11_autograph_staging.sql 의 부분 unique index 와 일치.

gate_status 결정:
  confidence ≥ 0.80 → auto_accept
  0.65 ≤ conf < 0.80 → needs_review
  conf < 0.65        → rejected   (저장은 함 — reject_memory 패턴과 함께 학습용)
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name


log = logging.getLogger(__name__)


CONF_AUTO_ACCEPT = 0.80
CONF_NEEDS_REVIEW = 0.65


def _gate(conf: float) -> str:
    if conf >= CONF_AUTO_ACCEPT:
        return "auto_accept"
    if conf >= CONF_NEEDS_REVIEW:
        return "needs_review"
    return "rejected"


def upsert_staging(rels: Iterable[dict], *, extractor_name: str,
                   extractor_version: str) -> dict[str, int]:
    """rels (engine 의 merge 후 dict 리스트) → staging UPSERT.

    같은 merge key 가 다시 등장하면 (e.g. 다른 청크에서 같은 관계) confidence 가 더
    높은 쪽으로 UPDATE 하고 evidence_chunk_ids 를 누적.
    """
    counts = {"auto_accept": 0, "needs_review": 0, "rejected": 0, "errors": 0}
    conn = get_connection()
    with conn.cursor() as cur:
        for r in rels:
            try:
                head_text = (r.get("head") or "").strip()
                tail_text = (r.get("tail") or "").strip()
                if not head_text or not tail_text:
                    continue
                rel_type = r.get("relation")
                head_kind = r.get("head_kind") or ""
                tail_kind = r.get("tail_kind") or ""
                if not rel_type or not head_kind or not tail_kind:
                    continue

                conf = float(r.get("confidence") or 0.0)
                gate = _gate(conf)
                counts[gate] = counts.get(gate, 0) + 1

                evidence = r.get("evidence") or ""
                evidences = r.get("_evidences") or ([evidence] if evidence else [])
                chunk_id = r.get("_chunk_id")
                chunk_ids = [int(chunk_id)] if chunk_id is not None else []
                snap_yr = r.get("_snapshot_year")
                try:
                    snap_yr = int(snap_yr) if snap_yr else None
                except (TypeError, ValueError):
                    snap_yr = None

                cur.execute("""
                    INSERT INTO auto.staging_relations
                      (relation_type, head_kind, head_text_norm, tail_kind, tail_text_norm,
                       snapshot_year, head_pg_id, tail_pg_id,
                       head_text, tail_text,
                       confidence_score, evidence_text, evidence_chunk_ids,
                       extractor_name, extractor_version, gate_status, raw)
                    VALUES (%s, %s, %s, %s, %s, %s,
                            NULL, NULL,
                            %s, %s,
                            %s, %s, %s,
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
                      evidence_chunk_ids = (
                        SELECT array_agg(DISTINCT x) FROM unnest(
                          auto.staging_relations.evidence_chunk_ids
                          || EXCLUDED.evidence_chunk_ids) x),
                      gate_status = CASE
                          WHEN EXCLUDED.confidence_score
                               > auto.staging_relations.confidence_score
                          THEN EXCLUDED.gate_status
                          ELSE auto.staging_relations.gate_status END
                """, (
                    rel_type, head_kind, normalize_corp_name(head_text),
                    tail_kind, normalize_corp_name(tail_text),
                    snap_yr,
                    head_text, tail_text,
                    conf, "\n---\n".join(evidences),
                    chunk_ids,
                    extractor_name, extractor_version, gate,
                    json.dumps(r, ensure_ascii=False, default=str),
                ))
            except Exception as e:  # noqa: BLE001
                counts["errors"] += 1
                log.warning("[staging] upsert failed for %s: %s", r.get("relation"), e)
    conn.commit()
    log.info("[staging] auto_accept=%d needs_review=%d rejected=%d errors=%d",
             counts["auto_accept"], counts["needs_review"], counts["rejected"],
             counts["errors"])
    return counts


__all__ = ["upsert_staging", "CONF_AUTO_ACCEPT", "CONF_NEEDS_REVIEW"]
