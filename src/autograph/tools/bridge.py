"""Cross-Domain bridge tool — FinGraph corp_code ↔ AutoGraph entity_id.

자유 SQL 금지. 사전 정의 함수만.

함수:
- bridge_corp_to_entity(corp_code, entity_type=None) — corp_code 가 매핑된 자동차 entity 목록
- bridge_entity_to_corp(entity_id, entity_type)     — 자동차 entity 의 corp_code
- cross_query(...)                                  — finance↔auto join helper (간단 wrapper)

reviewed_status='rejected' 는 항상 제외.
"""

from __future__ import annotations

from typing import Any

from ._db import query_dicts


VALID_ENTITY_TYPES = ("manufacturer", "supplier", "vehicle_model", "variant")


def bridge_corp_to_entity(corp_code: str, *,
                          entity_type: str | None = None,
                          min_confidence: float = 0.0,
                          include_candidate: bool = True) -> list[dict]:
    """corp_code 가 매핑된 자동차 entity 목록 (reviewed/candidate)."""
    if not corp_code:
        return []
    if entity_type and entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"entity_type 허용값: {VALID_ENTITY_TYPES}")

    return query_dicts("""
        SELECT entity_id, entity_type, name, wikidata_qid,
               match_method, confidence_score, reviewed_status,
               valid_from, valid_to
          FROM bridge.corp_entity
         WHERE corp_code = %s
           AND (%s::text IS NULL OR entity_type = %s)
           AND confidence_score >= %s
           AND reviewed_status <> 'rejected'
           AND (%s OR reviewed_status = 'reviewed')
         ORDER BY confidence_score DESC
    """, (corp_code, entity_type, entity_type, min_confidence, include_candidate))


def bridge_entity_to_corp(entity_id: str, entity_type: str,
                          *, include_candidate: bool = True) -> list[dict]:
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"entity_type 허용값: {VALID_ENTITY_TYPES}")
    return query_dicts("""
        SELECT corp_code, name, match_method, confidence_score, reviewed_status,
               valid_from, valid_to
          FROM bridge.corp_entity
         WHERE entity_id = %s
           AND entity_type = %s
           AND corp_code IS NOT NULL
           AND reviewed_status <> 'rejected'
           AND (%s OR reviewed_status = 'reviewed')
         ORDER BY confidence_score DESC
    """, (str(entity_id), entity_type, include_candidate))


def cross_query(*, corp_code: str | None = None,
                manufacturer_id: int | None = None,
                target: str = "manufacturer") -> dict[str, Any]:
    """간단한 cross-domain helper — corp ↔ manufacturer round-trip.

    - corp_code 가 주어지면 → 해당 corp 의 manufacturer entity 들
    - manufacturer_id 가 주어지면 → 해당 manufacturer 가 매핑된 corp_code(s)
    """
    out: dict[str, Any] = {"corp_code": corp_code,
                            "manufacturer_id": manufacturer_id,
                            "target": target}
    if corp_code:
        out["entities"] = bridge_corp_to_entity(corp_code, entity_type=target)
    if manufacturer_id is not None:
        out["corps"] = bridge_entity_to_corp(str(manufacturer_id), target)
    return out


__all__ = [
    "bridge_corp_to_entity",
    "bridge_entity_to_corp",
    "cross_query",
]
