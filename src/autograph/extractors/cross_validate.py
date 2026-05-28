"""P4 cross-validate — auto.staging_relations → Neo4j 적재 결정.

규칙 (PRD §3.5, §6.7):
  - 정형 P2 와 일치 → validated (confidence boost = max(c_llm, 0.95))
  - 정형 P2 와 충돌 → reject (deterministic SSOT 우선)
  - 정형 P2 에 없으나 confidence ≥ 0.80 → candidate (그래프에 적재 + validated_status='candidate')
  - 0.65 ≤ conf < 0.80 → needs_review (그래프 적재 + flag)
  - conf < 0.65 → reject_memory (적재 안 함)

본 PR 의 적재 대상 관계:
  SUPPLIED_BY (Module|Part → Supplier)
  RECALL_OF   (Recall → Module|Part)

CLI:
    python -m autograph.extractors.cross_validate
    python -m autograph.extractors.cross_validate --dry-run
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection

from ..loaders._neo4j_helpers import run_batched


log = logging.getLogger(__name__)


@dataclass
class P4Stats:
    seen:        int = 0
    validated:   int = 0
    candidate:   int = 0
    needs_review: int = 0
    rejected:    int = 0
    written:     int = 0
    errors: list[str] = field(default_factory=list)


def _resolve_component_id(cur, name_norm: str) -> tuple[int, int] | None:
    """name_norm → (component_id, level). 다중 매칭이면 가장 짧은 이름 (가장 일반).

    level 은 그래프 라벨 선택에 필요 (4=Module, 5=Part).
    """
    cur.execute("""
        SELECT component_id, level
          FROM auto.components
         WHERE name_norm = %s
         ORDER BY length(canonical_name) ASC
         LIMIT 1
    """, (name_norm,))
    r = cur.fetchone()
    return (r[0], r[1]) if r else None


def _resolve_supplier_id(cur, name_norm: str) -> int | None:
    cur.execute("""
        SELECT supplier_id FROM auto.master_suppliers
         WHERE name_norm = %s LIMIT 1
    """, (name_norm,))
    r = cur.fetchone()
    return r[0] if r else None


def _resolve_recall_id(cur, head_text_norm: str) -> int | None:
    """LLM 이 추출한 head 가 recall 의 source_recall_no 또는 component_text 였을 수 있다."""
    cur.execute("""
        SELECT recall_id FROM auto.events_recalls
         WHERE source_recall_no = %s
            OR LOWER(component_text) = %s
         LIMIT 1
    """, (head_text_norm, head_text_norm))
    r = cur.fetchone()
    return r[0] if r else None


# (:Module|:Part)-[:SUPPLIED_BY]->(:Supplier) — staging 에서 발생.
_MERGE_SUPPLIED_BY = """
UNWIND $rows AS r
MATCH (c) WHERE c.id = r.component_id AND (c:Module OR c:Part)
MATCH (s:Supplier {entity_id: r.supplier_entity_id})
MERGE (c)-[rel:SUPPLIED_BY]->(s)
SET   rel.source_type      = 'llm_p3',
      rel.source_id        = r.source_id,
      rel.extraction_method= 'llm',
      rel.confidence_score = r.confidence_score,
      rel.validated_status = r.validated_status,
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year),
      rel.evidence         = r.evidence_text,
      rel.p3_chunk_ids     = r.evidence_chunk_ids
"""


_MERGE_RECALL_OF = """
UNWIND $rows AS r
MATCH (rc:Recall {id: r.recall_id})
MATCH (c) WHERE c.id = r.component_id AND (c:Module OR c:Part)
MERGE (rc)-[rel:RECALL_OF]->(c)
SET   rel.source_type      = 'llm_p3',
      rel.source_id        = r.source_id,
      rel.extraction_method= 'llm',
      rel.confidence_score = r.confidence_score,
      rel.validated_status = r.validated_status,
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year),
      rel.evidence         = r.evidence_text,
      rel.p3_chunk_ids     = r.evidence_chunk_ids
"""


def _validate_supplied_by(cur, row: dict) -> tuple[str, dict | None]:
    """SUPPLIED_BY staging row 검증.

    Returns:
        (decision, neo4j_payload)
        decision ∈ {'validated','candidate','needs_review','rejected'}
        neo4j_payload 는 적재 대상일 때만 dict, 아니면 None.
    """
    head_norm = row["head_text_norm"]
    tail_norm = row["tail_text_norm"]
    conf = float(row["confidence_score"])

    if conf < 0.65:
        return "rejected", None

    comp = _resolve_component_id(cur, head_norm)
    sup_id = _resolve_supplier_id(cur, tail_norm)
    if comp is None or sup_id is None:
        # 부품/공급사 매핑 실패 — 사람 검토.
        if conf >= 0.80:
            return "needs_review", None
        return "rejected", None

    component_id, level = comp
    payload = {
        "component_id": int(component_id),
        "supplier_entity_id": str(sup_id),
        "confidence_score": conf,
        "evidence_text": row.get("evidence_text"),
        "evidence_chunk_ids": list(row.get("evidence_chunk_ids") or []),
        "source_id": f"staging:{row['staging_id']}",
        "snapshot_year": row.get("snapshot_year"),
    }

    # 기존 deterministic (manual_supplier_seed) 와의 일치 검사 — Neo4j 에서 직접 확인.
    # 일치하면 confidence 부스팅 + validated.
    with get_driver().session() as session:
        rec = session.run(
            """
            MATCH (c)-[r:SUPPLIED_BY]->(s:Supplier {entity_id: $sid})
             WHERE c.id = $cid AND (c:Module OR c:Part)
            RETURN r.source_type AS source_type, r.confidence_score AS conf
            """, cid=component_id, sid=str(sup_id),
        ).single()
    if rec:
        existing_src = rec["source_type"]
        if existing_src and existing_src != "llm_p3":
            # 결정적 출처와 일치 → validated, conf 부스팅.
            payload["confidence_score"] = max(conf, float(rec["conf"] or 0), 0.95)
            payload["validated_status"] = "validated"
            return "validated", payload

    # 결정적 매칭 없음 — confidence 만 보고 candidate/needs_review.
    if conf >= 0.80:
        payload["validated_status"] = "candidate"
        return "candidate", payload
    payload["validated_status"] = "needs_review"
    return "needs_review", payload


def _validate_recall_of(cur, row: dict) -> tuple[str, dict | None]:
    """RECALL_OF staging row 검증."""
    head_norm = row["head_text_norm"]
    tail_norm = row["tail_text_norm"]
    conf = float(row["confidence_score"])
    if conf < 0.65:
        return "rejected", None

    recall_id = _resolve_recall_id(cur, head_norm)
    comp = _resolve_component_id(cur, tail_norm)
    if recall_id is None or comp is None:
        if conf >= 0.80:
            return "needs_review", None
        return "rejected", None

    component_id, _ = comp
    payload = {
        "recall_id": int(recall_id),
        "component_id": int(component_id),
        "confidence_score": conf,
        "evidence_text": row.get("evidence_text"),
        "evidence_chunk_ids": list(row.get("evidence_chunk_ids") or []),
        "source_id": f"staging:{row['staging_id']}",
        "snapshot_year": row.get("snapshot_year"),
    }
    # P2 deterministic 매칭 존재 시 boost — load_recall_components 가 같은 pair 를 썼는지 확인.
    cur.execute("""
        SELECT 1 FROM auto.events_recalls
         WHERE recall_id = %s AND component_id = %s
    """, (recall_id, component_id))
    if cur.fetchone():
        payload["confidence_score"] = max(conf, 0.95)
        payload["validated_status"] = "validated"
        return "validated", payload
    if conf >= 0.80:
        payload["validated_status"] = "candidate"
        return "candidate", payload
    payload["validated_status"] = "needs_review"
    return "needs_review", payload


_VALIDATORS = {
    "SUPPLIED_BY": _validate_supplied_by,
    "RECALL_OF":   _validate_recall_of,
}

_CYPHER_BY_REL = {
    "SUPPLIED_BY": _MERGE_SUPPLIED_BY,
    "RECALL_OF":   _MERGE_RECALL_OF,
}


def _fetch_staging(cur, *, only_pending: bool = True) -> list[dict]:
    sql = """
        SELECT staging_id, relation_type, head_kind, head_text_norm,
               tail_kind, tail_text_norm, snapshot_year,
               head_text, tail_text, confidence_score, evidence_text,
               evidence_chunk_ids, gate_status, p4_decision
          FROM auto.staging_relations
    """
    if only_pending:
        sql += " WHERE p4_decision IS NULL AND gate_status <> 'rejected'"
    cur.execute(sql)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def run_p4(*, dry_run: bool = False, batch: int = 200) -> P4Stats:
    stats = P4Stats()
    conn = get_connection()
    with conn.cursor() as cur:
        rows = _fetch_staging(cur)

    # 그룹별 (관계 타입) 적재 큐.
    payloads_by_rel: dict[str, list[dict]] = {}
    decisions: list[tuple[int, str, str]] = []   # (staging_id, decision, reason)

    with conn.cursor() as cur:
        for r in rows:
            stats.seen += 1
            rt = r["relation_type"]
            validator = _VALIDATORS.get(rt)
            if not validator:
                decisions.append((r["staging_id"], "rejected",
                                  f"unknown_relation_type:{rt}"))
                stats.rejected += 1
                continue
            try:
                decision, payload = validator(cur, r)
            except Exception as e:  # noqa: BLE001
                stats.errors.append(f"{rt}/{r['staging_id']}: {e}")
                decisions.append((r["staging_id"], "rejected", f"error:{e}"))
                stats.rejected += 1
                continue

            if decision == "validated":
                stats.validated += 1
            elif decision == "candidate":
                stats.candidate += 1
            elif decision == "needs_review":
                stats.needs_review += 1
            else:
                stats.rejected += 1

            decisions.append((r["staging_id"], decision, payload and "ok" or "no_payload"))
            if payload is not None and decision in ("validated", "candidate", "needs_review"):
                payloads_by_rel.setdefault(rt, []).append(payload)

    # PG 업데이트 — p4_decision 기록.
    with conn.cursor() as cur:
        cur.executemany("""
            UPDATE auto.staging_relations
               SET p4_decision = %s,
                   p4_reason   = %s,
                   p4_at       = now()
             WHERE staging_id  = %s
        """, [(d, reason, sid) for sid, d, reason in decisions])

    if dry_run:
        conn.rollback()
        log.info("[p4] DRY-RUN seen=%d val=%d cand=%d review=%d reject=%d",
                 stats.seen, stats.validated, stats.candidate,
                 stats.needs_review, stats.rejected)
        return stats
    conn.commit()

    # Neo4j 적재 — 관계 타입별 cypher.
    if payloads_by_rel:
        driver = get_driver()
        with driver.session() as session:
            for rt, payloads in payloads_by_rel.items():
                cypher = _CYPHER_BY_REL.get(rt)
                if not cypher:
                    continue
                stats.written += run_batched(session, cypher, payloads, batch=batch)
                # neo4j_loaded_at 갱신.
                conn = get_connection()
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE auto.staging_relations
                           SET neo4j_loaded_at = now()
                         WHERE staging_id IN (
                           SELECT staging_id FROM auto.staging_relations
                            WHERE relation_type = %s
                              AND p4_decision IN ('validated','candidate','needs_review')
                              AND neo4j_loaded_at IS NULL
                         )
                    """, (rt,))
                conn.commit()

    log.info("[p4] seen=%d val=%d cand=%d review=%d reject=%d written=%d errors=%d",
             stats.seen, stats.validated, stats.candidate, stats.needs_review,
             stats.rejected, stats.written, len(stats.errors))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.extractors.cross_validate")
    ap.add_argument("--batch", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_p4(dry_run=args.dry_run, batch=args.batch)


if __name__ == "__main__":
    main()


__all__ = ["run_p4", "P4Stats"]
