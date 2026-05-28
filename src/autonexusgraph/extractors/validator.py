"""P4 — P3 LLM 산출 cross-validate.

원칙 (PRD §6.5):
- 정형 데이터(P2) 가 SSOT. P3 가 만든 관계가 P2 와 충돌하면 P3 폐기 / review queue.
- P3 가 만든 관계가 P2 와 일치하면 confidence 부스팅 (validated=True 표시).
- P3 가 P2 에 없는 새 관계를 만들면, confidence 만 보고 적재 여부 결정.

검증 규칙 (relation 단위):
- PARTNER_OF (A,B): A 와 B 가 같은 group / parent 면 정상. SUBSIDIARY_OF 관계가 이미 있으면 폐기 (자회사를 협력사로 만들지 말 것).
- COMPETES_WITH (A,B): SUBSIDIARY_OF / PARTNER_OF 가 동시에 존재하면 confidence 감점.
- INVESTED_IN (A,B): ownership_pct 가 명시되었고 50%+ 이면 P2 의 SUBSIDIARY_OF 와 충돌 → 폐기 또는 P2 보강 후보 (review).
- PRODUCES (A,P): 상대가 Product 노드 — 충돌 룰 없음. confidence 만 검토.

산출:
- validated_relations 리스트 — Neo4j 적재용
- review_queue 리스트 — 사람 검토 필요
- discarded 리스트 — 충돌로 폐기 (감사용)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from ..db.neo4j import get_driver


log = logging.getLogger(__name__)


CONF_ACCEPT_DEFAULT = 0.70
CONF_REVIEW_DEFAULT = 0.50


@dataclass
class ValidationResult:
    """단일 관계의 검증 결과."""
    rel: dict                # 원본 P3 relation dict
    decision: str            # 'accept' | 'review' | 'discard'
    reason: str              # 의사결정 사유
    final_confidence: float


def _resolve_corp_codes(names: Iterable[str]) -> dict[str, str | None]:
    """회사명 → corp_code 매핑 (master.company_aliases 통해).

    1차: alias_norm 정확 매칭. 2차: 부분 매칭은 신뢰도 낮으므로 skip.
    """
    from ..db.postgres import get_pool
    from ..ingestion._common import normalize_corp_name

    names_norm = {n: normalize_corp_name(n) for n in names if n}
    if not names_norm:
        return {}

    sql = """
    SELECT alias_norm, corp_code
      FROM master.company_aliases
     WHERE alias_norm = ANY(%s)
    """
    params = [list(names_norm.values())]
    out: dict[str, str | None] = {}
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        norm_to_corp = {row[0]: row[1] for row in cur.fetchall()}

    for original, norm in names_norm.items():
        out[original] = norm_to_corp.get(norm)
    return out


def _check_conflict(head_corp: str | None, tail_corp: str | None,
                    relation_type: str) -> str | None:
    """Neo4j 에서 head-tail 사이 기존 P2 관계 검사 → 충돌 사유 반환 (None 이면 충돌 없음)."""
    if not head_corp or not tail_corp:
        return None
    cypher = """
    MATCH (a:Company {corp_code: $a})
    MATCH (b:Company {corp_code: $b})
    OPTIONAL MATCH (a)-[r1:SUBSIDIARY_OF|RELATED_TO]->(b)
    OPTIONAL MATCH (b)-[r2:SUBSIDIARY_OF|RELATED_TO]->(a)
    RETURN type(r1) AS r1, type(r2) AS r2
    """
    with get_driver().session() as session:
        rec = session.run(cypher, a=head_corp, b=tail_corp).single()
    if not rec:
        return None
    r1, r2 = rec["r1"], rec["r2"]

    # PARTNER_OF 가 SUBSIDIARY_OF 와 충돌
    if relation_type == "PARTNER_OF":
        if r1 == "SUBSIDIARY_OF" or r2 == "SUBSIDIARY_OF":
            return "subsidiary relation already exists (P2 SSOT)"
    # COMPETES_WITH 가 자회사 관계와 충돌
    if relation_type == "COMPETES_WITH":
        if r1 == "SUBSIDIARY_OF" or r2 == "SUBSIDIARY_OF":
            return "subsidiary relation exists — cannot also compete"
    return None


def validate_relations(
    p3_results: list[dict],
    *,
    accept_threshold: float = CONF_ACCEPT_DEFAULT,
    review_threshold: float = CONF_REVIEW_DEFAULT,
) -> dict[str, list[ValidationResult]]:
    """P3 산출 리스트 → {accept, review, discard} 분류.

    p3_results: extract_one 의 P3Result.relations 들을 합친 리스트 (각 원소에 head/tail/relation/confidence/evidence).
    Neo4j 적재는 별도 loader 가 accept 그룹만 처리.
    """
    if not p3_results:
        return {"accept": [], "review": [], "discard": []}

    # head/tail 회사명 일괄 corp_code 매핑
    names: set[str] = set()
    for r in p3_results:
        if r.get("head"):
            names.add(r["head"])
        if r.get("tail"):
            names.add(r["tail"])
    name_to_corp = _resolve_corp_codes(names)

    accept: list[ValidationResult] = []
    review: list[ValidationResult] = []
    discard: list[ValidationResult] = []

    for rel in p3_results:
        rtype = rel.get("relation")
        conf = float(rel.get("confidence") or 0.0)
        head_corp = name_to_corp.get(rel.get("head", ""))
        tail_corp = name_to_corp.get(rel.get("tail", ""))

        # PRODUCES 는 Product 대상 — corp resolve 불가능. confidence 만 본다.
        if rtype == "PRODUCES":
            decision = "accept" if conf >= accept_threshold else (
                "review" if conf >= review_threshold else "discard"
            )
            res = ValidationResult(rel=rel, decision=decision,
                                    reason=f"PRODUCES conf-only", final_confidence=conf)
            (accept if decision == "accept" else
             review if decision == "review" else discard).append(res)
            continue

        # 회사 양쪽 resolve 안 되면 외부 회사 — confidence 만 보고 review 또는 discard.
        if not head_corp and not tail_corp:
            decision = "review" if conf >= review_threshold else "discard"
            res = ValidationResult(rel=rel, decision=decision,
                                    reason="both unresolved", final_confidence=conf * 0.7)
            (review if decision == "review" else discard).append(res)
            continue

        # 충돌 검사 (head-tail 양쪽 resolve 된 경우만 의미 있음)
        conflict = None
        if head_corp and tail_corp:
            conflict = _check_conflict(head_corp, tail_corp, rtype)
            if conflict:
                discard.append(ValidationResult(
                    rel=rel, decision="discard",
                    reason=conflict, final_confidence=0.0,
                ))
                continue

        # corp 매핑 + 충돌 없음 → confidence 부스팅
        boosted = min(1.0, conf + 0.10) if (head_corp and tail_corp) else conf
        decision = "accept" if boosted >= accept_threshold else (
            "review" if boosted >= review_threshold else "discard"
        )
        res = ValidationResult(
            rel={**rel, "head_corp_code": head_corp, "tail_corp_code": tail_corp},
            decision=decision,
            reason="ok" if not conflict else conflict,
            final_confidence=boosted,
        )
        (accept if decision == "accept" else
         review if decision == "review" else discard).append(res)

    log.info(f"[p4] accept={len(accept)} review={len(review)} discard={len(discard)}")
    return {"accept": accept, "review": review, "discard": discard}


__all__ = [
    "CONF_ACCEPT_DEFAULT", "CONF_REVIEW_DEFAULT",
    "ValidationResult",
    "validate_relations",
]
