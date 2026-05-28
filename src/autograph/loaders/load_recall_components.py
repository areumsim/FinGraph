"""(:Recall)-[:RECALL_OF]->(:Module|:Part) 결정적 매칭 + Neo4j 적재.

NHTSA / KOTSA 리콜의 ``component_text`` 자유 텍스트 (예 "AIR BAGS:FRONTAL",
"POWER TRAIN:AUTOMATIC TRANSMISSION", "ELECTRICAL SYSTEM:WIRING:HOSES")
를 ``auto.components`` (Module/Part) 의 canonical_name / aliases / name_norm 에
정규화 매칭. PG 의 events_recalls.component_id 를 채우고, Neo4j 에 RECALL_OF
엣지를 emit.

매칭 강도:
  exact  (정규화 후 동일)            → confidence 0.85
  alias  (component.aliases 에 포함) → confidence 0.80
  token  (의미 토큰 1 개 이상 일치)  → confidence 0.65 — 가중치 낮음
  no_match                            → events_recalls.component_id NULL 유지
                                        (P3 LLM 단계가 채움)

CLI:
    python -m autograph.loaders.load_recall_components
    python -m autograph.loaders.load_recall_components --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field

from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name

from ._neo4j_helpers import run_batched


log = logging.getLogger(__name__)


# 매칭에서 제거할 일반 영문 noise (NHTSA component_text 의 "SYSTEM" 등 너무 일반적).
_STOP_TOKENS = frozenset({
    "system", "systems", "component", "components", "assembly", "assy",
    "the", "of", "and", "or", "with", "without",
})


_STEM_SUFFIXES = ("ings", "ing", "ies", "es", "s")


def _stem(tok: str) -> str:
    """초경량 영문 stem — 's'/'es'/'ing' 등 어미 제거 (≥4글자 만).

    NHTSA 의 'AIR BAGS' vs catalog 의 'Air Bag', 'WIRING' vs 'Wire Harness'
    같은 단순 어형 차이를 흡수. 한국어/4글자 이하 토큰은 그대로.
    """
    if len(tok) <= 3:
        return tok
    for suf in _STEM_SUFFIXES:
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)]
    return tok


def _tokenize(s: str | None) -> list[str]:
    """component_text → 의미 토큰 리스트 (소문자, stop 제거, 기본 stem)."""
    if not s:
        return []
    # 'AIR BAGS:FRONTAL' → ['air','bag','frontal']
    raw = [t for t in re.split(r"[\s:/,\(\)\-]+", s.lower().strip()) if t]
    return [_stem(t) for t in raw if t and t not in _STOP_TOKENS]


@dataclass
class MatchStats:
    recalls_scanned:    int = 0
    matched_exact:      int = 0
    matched_alias:      int = 0
    matched_token:      int = 0
    no_match:           int = 0
    edges_written:      int = 0
    errors: list[str]   = field(default_factory=list)


def _load_components(cur) -> list[dict]:
    """level 4/5 (Module + Part) 의 canonical_name + name_norm + aliases."""
    cur.execute("""
        SELECT component_id, canonical_name, name_norm, aliases, level
          FROM auto.components
         WHERE level IN (4, 5)
    """)
    out: list[dict] = []
    for r in cur.fetchall():
        out.append({
            "id": r[0],
            "name": r[1],
            "name_norm": r[2],
            "aliases": list(r[3] or []),
            "level": r[4],
            "tokens": set(_tokenize(r[1]) + _tokenize(" ".join(r[3] or []))),
        })
    return out


def _match_one(text: str, components: list[dict]
               ) -> tuple[dict | None, str, float]:
    """text → (matched_component, match_kind, confidence). 매칭 실패 시 (None, '', 0)."""
    if not text:
        return None, "", 0.0
    text_norm = normalize_corp_name(text)
    text_tokens = set(_tokenize(text))
    if not text_tokens:
        return None, "", 0.0

    best_token: tuple[dict | None, int] = (None, 0)
    for c in components:
        if c["name_norm"] == text_norm:
            return c, "exact", 0.85
        # alias 정확 매칭
        for a in c["aliases"]:
            if normalize_corp_name(a) == text_norm:
                return c, "alias", 0.80
        # token 교집합 크기 추적
        overlap = len(c["tokens"] & text_tokens)
        if overlap > best_token[1]:
            best_token = (c, overlap)

    # 의미 토큰 ≥ 2 일치 — token 매칭. 한쪽이 1 토큰이면 ≥ 1 로 완화.
    c, n = best_token
    if c is None or n == 0:
        return None, "", 0.0
    threshold = 1 if (len(c["tokens"]) <= 1 or len(text_tokens) <= 1) else 2
    if n >= threshold:
        return c, "token", 0.65
    return None, "", 0.0


# Neo4j 엣지 적재 — UNWIND $rows AS r, MATCH 후 MERGE rel + §6.7 메타.
# (rc:Recall)-[:RECALL_OF]->(c:Module|Part)
_MERGE_RECALL_OF = """
UNWIND $rows AS r
MATCH (rc:Recall {id: r.recall_id})
OPTIONAL MATCH (c) WHERE c.id = r.component_id AND (c:Module OR c:Part)
WITH rc, r, c WHERE c IS NOT NULL
MERGE (rc)-[rel:RECALL_OF]->(c)
SET   rel.source_type      = r.source_type,
      rel.source_id        = r.source_id,
      rel.extraction_method= r.extraction_method,
      rel.confidence_score = r.confidence_score,
      rel.validated_status = r.validated_status,
      rel.snapshot_year    = coalesce(r.snapshot_year, date().year),
      rel.match_kind       = r.match_kind
"""


def load_recall_components(*, dry_run: bool = False, batch: int = 500) -> MatchStats:
    stats = MatchStats()
    conn = get_connection()
    edges: list[dict] = []

    with conn.cursor() as cur:
        components = _load_components(cur)
        if not components:
            log.warning("[recall→comp] auto.components 비어있음 — 매칭 불가")
            return stats

        cur.execute("""
            SELECT recall_id, source, source_recall_no, component_text, snapshot_year
              FROM auto.events_recalls
             WHERE component_text IS NOT NULL
        """)
        rows = cur.fetchall()

    # 매칭은 read-only 트랜잭션 외부에서 수행 (큰 components 배열 메모리 보유).
    log.info("[recall→comp] scanning %d recalls vs %d components", len(rows), len(components))
    matched_ids: list[tuple[int, int, str, float]] = []     # (recall_id, comp_id, kind, conf)
    for r in rows:
        recall_id, source, source_no, text, snap_yr = r
        stats.recalls_scanned += 1
        c, kind, conf = _match_one(text, components)
        if c is None:
            stats.no_match += 1
            continue
        if kind == "exact":   stats.matched_exact += 1
        elif kind == "alias": stats.matched_alias += 1
        else:                 stats.matched_token += 1
        matched_ids.append((recall_id, int(c["id"]), kind, conf))
        edges.append({
            "recall_id": recall_id, "component_id": int(c["id"]),
            "source_type": f"pg.auto.events_recalls/{source}",
            "source_id": source_no,
            "extraction_method": "deterministic",
            "confidence_score": conf,
            "validated_status": "verified" if kind == "exact" else "candidate",
            "snapshot_year": snap_yr,
            "match_kind": kind,
        })

    if matched_ids:
        with conn.cursor() as cur:
            cur.executemany("""
                UPDATE auto.events_recalls
                   SET component_id = %s
                 WHERE recall_id    = %s
                   AND component_id IS DISTINCT FROM %s
            """, [(cid, rid, cid) for rid, cid, _, _ in matched_ids])

    if dry_run:
        conn.rollback()
        log.info("[recall→comp] DRY-RUN: would update %d PG rows, emit %d edges",
                 len(matched_ids), len(edges))
        return stats
    conn.commit()

    # Neo4j 적재.
    if edges:
        driver = get_driver()
        with driver.session() as session:
            n = run_batched(session, _MERGE_RECALL_OF, edges, batch=batch)
            stats.edges_written = n

    log.info("[recall→comp] scanned=%d exact=%d alias=%d token=%d no_match=%d edges=%d",
             stats.recalls_scanned, stats.matched_exact, stats.matched_alias,
             stats.matched_token, stats.no_match, stats.edges_written)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_recall_components")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_recall_components(dry_run=args.dry_run, batch=args.batch)


if __name__ == "__main__":
    main()


__all__ = ["load_recall_components", "MatchStats"]
