"""자동차 텍스트 청크 → vec.chunks 적재.

대상:
- nhtsa_recalls 의 Summary / Consequence / Remedy 본문
- nhtsa_complaints 의 summary 본문
- (옵션) wikipedia_auto 본문 — 별도 ingestion 필요

청크 단위:
- 보고서 1건당 1청크 (작아서 분리 불필요). token_count 는 단순 char/4 추정.
- source: 'nhtsa_recall' | 'nhtsa_complaint'
- 메타: source_recall_no / source_complaint_no, manufacturer_id, model_id, variant_id

embedding 은 본 모듈에서 호출하지 않음 — 기존 finance 와 동일하게 별도
`make embed-chunks` 등으로 BGE-M3 호출 후 backfill.

CLI:
    python -m autograph.loaders.build_chunks_auto
    python -m autograph.loaders.build_chunks_auto --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging

from autonexusgraph.db.postgres import get_connection


log = logging.getLogger(__name__)


# vec.chunks 의 rcept_no/section/chunk_idx UNIQUE(rcept_no, chunk_idx) 가 있어
# 자동차 청크는 rcept_no=NULL → unique 충돌 회피 위해 metadata.uniq 키 활용.
# section='auto.recall'|'auto.complaint' 로 구분.
def _upsert_chunk(cur, *, source: str, section: str, text: str,
                  metadata: dict,
                  manufacturer_id: int | None,
                  model_id: int | None,
                  variant_id: int | None) -> None:
    # corp_code 는 nullable 로 완화됨 (09 migration). source_uniq 를 metadata 에 박아 dedup.
    uniq = metadata.get("uniq")
    if not uniq:
        raise ValueError("metadata['uniq'] 필요")

    cur.execute("""
        SELECT id, manufacturer_id, model_id, variant_id FROM vec.chunks
        WHERE source = %s AND metadata->>'uniq' = %s
        LIMIT 1
    """, (source, uniq))
    existing = cur.fetchone()
    if existing:
        # 기존 row 의 NULL 메타만 보강 (이미 채워진 값은 보존).
        cid, ex_mfr, ex_model, ex_variant = existing
        if (manufacturer_id and not ex_mfr) or (model_id and not ex_model) or (variant_id and not ex_variant):
            cur.execute("""
                UPDATE vec.chunks
                   SET manufacturer_id = COALESCE(manufacturer_id, %s),
                       model_id        = COALESCE(model_id, %s),
                       variant_id      = COALESCE(variant_id, %s)
                 WHERE id = %s
            """, (manufacturer_id, model_id, variant_id, cid))
        return

    token_est = max(1, len(text) // 4)
    cur.execute("""
        INSERT INTO vec.chunks
          (corp_code, rcept_no, section, chunk_idx, text, token_count,
           metadata, source, manufacturer_id, model_id, variant_id)
        VALUES (NULL, NULL, %s, 0, %s, %s,
                %s::jsonb, %s, %s, %s, %s)
    """, (section, text, token_est,
          json.dumps(metadata, ensure_ascii=False, default=str),
          source, manufacturer_id, model_id, variant_id))


def build_from_recalls() -> int:
    conn = get_connection()
    n = 0
    with conn.cursor() as cur:
        cur.execute("""
            SELECT recall_id, source_recall_no, manufacturer_id, model_id, variant_id,
                   component_text, defect_summary, consequence, remedy_summary,
                   report_date
              FROM auto.events_recalls
        """)
        rows = cur.fetchall()
    with conn.cursor() as cur:
        for r in rows:
            (recall_id, no, mfr_id, model_id, variant_id,
             comp, defect, conseq, remedy, rdate) = r
            text_parts = []
            if comp:    text_parts.append(f"부품: {comp}")
            if defect:  text_parts.append(f"결함: {defect}")
            if conseq:  text_parts.append(f"위험: {conseq}")
            if remedy:  text_parts.append(f"조치: {remedy}")
            text = "\n".join(text_parts).strip()
            if not text:
                continue
            try:
                _upsert_chunk(cur,
                    source="nhtsa_recall",
                    section="auto.recall",
                    text=text,
                    metadata={
                        "uniq": f"nhtsa_recall::{no}",
                        "source_recall_no": no,
                        "report_date": rdate.isoformat() if rdate else None,
                    },
                    manufacturer_id=mfr_id,
                    model_id=model_id,
                    variant_id=variant_id)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("[chunks:recall] %s: %s", no, e)
    conn.commit()
    log.info("[chunks:recall] inserted=%d", n)
    return n


def build_from_complaints() -> int:
    conn = get_connection()
    n = 0
    with conn.cursor() as cur:
        cur.execute("""
            SELECT complaint_id, source_complaint_no, manufacturer_id, model_id, variant_id,
                   summary, filed_date
              FROM auto.events_complaints
        """)
        rows = cur.fetchall()
    with conn.cursor() as cur:
        for r in rows:
            (cid, no, mfr_id, model_id, variant_id, summary, fdate) = r
            if not summary:
                continue
            try:
                _upsert_chunk(cur,
                    source="nhtsa_complaint",
                    section="auto.complaint",
                    text=summary,
                    metadata={
                        "uniq": f"nhtsa_complaint::{no}",
                        "filed_date": fdate.isoformat() if fdate else None,
                    },
                    manufacturer_id=mfr_id,
                    model_id=model_id,
                    variant_id=variant_id)
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("[chunks:complaint] %s: %s", no, e)
    conn.commit()
    log.info("[chunks:complaint] inserted=%d", n)
    return n


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.build_chunks_auto")
    ap.add_argument("--source",
                    choices=["recalls", "complaints", "all"], default="all")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.source in ("recalls", "all"):
        build_from_recalls()
    if args.source in ("complaints", "all"):
        build_from_complaints()


if __name__ == "__main__":
    main()
