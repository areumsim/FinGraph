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


def bridge_sec_cik_to_entity(sec_cik: str, *,
                             entity_type: str = "manufacturer") -> list[dict]:
    """SEC CIK → AutoGraph entity 매핑. 글로벌 OEM cross-domain 진입점.

    Tesla/Ford/GM/Stellantis 등 SEC 발행 OEM 의 financials 조회 전에 manufacturer_id 확보용.
    """
    if not sec_cik:
        return []
    if entity_type and entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"entity_type 허용값: {VALID_ENTITY_TYPES}")
    cik10 = str(sec_cik).strip().zfill(10)
    return query_dicts("""
        SELECT entity_id, entity_type, name, wikidata_qid, sec_cik,
               corp_code, match_method, confidence_score, reviewed_status
          FROM bridge.corp_entity
         WHERE sec_cik = %s
           AND (%s::text IS NULL OR entity_type = %s)
           AND reviewed_status <> 'rejected'
         ORDER BY confidence_score DESC
    """, (cik10, entity_type, entity_type))


def bridge_entity_to_sec_cik(entity_id: str | int,
                              entity_type: str = "manufacturer") -> list[dict]:
    """AutoGraph entity → SEC CIK. SEC EDGAR API 호출 준비용."""
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"entity_type 허용값: {VALID_ENTITY_TYPES}")
    return query_dicts("""
        SELECT sec_cik, name, corp_code, match_method, confidence_score, reviewed_status
          FROM bridge.corp_entity
         WHERE entity_id = %s
           AND entity_type = %s
           AND sec_cik IS NOT NULL
           AND reviewed_status <> 'rejected'
         ORDER BY confidence_score DESC
    """, (str(entity_id), entity_type))


def get_oem_financials_sec(manufacturer_id: int, *,
                           concept: str | None = None,
                           fiscal_period: str = "FY",
                           year_min: int | None = None,
                           year_max: int | None = None,
                           limit: int = 20) -> list[dict]:
    """글로벌 OEM 재무 (SEC XBRL facts). PRD §10 cross-domain QA 의 정량 답변용.

    concept 미지정 → 핵심 회계 항목 (Revenues, NetIncomeLoss, OperatingIncomeLoss …) 다중.
    fiscal_period: 'FY' (연간 기본) | 'Q1' | 'Q2' | 'Q3'.
    """
    return query_dicts("""
        SELECT concept, taxonomy, unit, fiscal_year, fiscal_period,
               period_end, value, form_type, accession_no, filed_at, confidence
          FROM auto.oem_financials_sec
         WHERE manufacturer_id = %s
           AND (%s::text IS NULL OR concept = %s)
           AND (%s::text IS NULL OR fiscal_period = %s)
           AND (%s::int  IS NULL OR fiscal_year >= %s::int)
           AND (%s::int  IS NULL OR fiscal_year <= %s::int)
           AND validated_status <> 'rejected'
         ORDER BY fiscal_year DESC, fiscal_period DESC, concept
         LIMIT %s
    """, (
        int(manufacturer_id),
        concept, concept,
        fiscal_period, fiscal_period,
        year_min, year_min,
        year_max, year_max,
        max(1, min(int(limit), 500)),
    ))


def cross_query(*, corp_code: str | None = None,
                entity_id: str | int | None = None,
                entity_type: str = "manufacturer",
                manufacturer_id: int | None = None,
                target: str | None = None) -> dict[str, Any]:
    """Cross-domain helper — corp_code ↔ AutoGraph entity (모든 entity_type).

    인자:
      corp_code     : FinGraph corp_code → 해당 corp 가 매핑된 자동차 entity 들
      entity_id     : AutoGraph 측 식별자 (variant_id/supplier_id/manufacturer_id …) →
                       매핑된 corp_code(s). 정수도 자동 stringify.
      entity_type   : 'manufacturer' | 'supplier' | 'vehicle_model' | 'variant'
                       (bridge.VALID_ENTITY_TYPES)

    Backward compat: ``manufacturer_id=...`` 와 ``target=...`` 옛 시그니처 그대로 사용 가능.
    """
    # 옛 시그니처 backward-compat 매핑.
    if target is not None and entity_type == "manufacturer":
        entity_type = target
    if manufacturer_id is not None and entity_id is None:
        entity_id = manufacturer_id
        # target 미명시 옛 호출은 manufacturer 그대로.
        if target is None:
            entity_type = "manufacturer"

    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"entity_type 허용값: {VALID_ENTITY_TYPES}")

    out: dict[str, Any] = {
        "corp_code": corp_code,
        "entity_id": entity_id,
        "entity_type": entity_type,
    }
    if corp_code:
        out["entities"] = bridge_corp_to_entity(corp_code, entity_type=entity_type)
    if entity_id is not None:
        out["corps"] = bridge_entity_to_corp(str(entity_id), entity_type)
    return out


__all__ = [
    "bridge_corp_to_entity",
    "bridge_entity_to_corp",
    "bridge_sec_cik_to_entity",
    "bridge_entity_to_sec_cik",
    "get_oem_financials_sec",
    "cross_query",
]
