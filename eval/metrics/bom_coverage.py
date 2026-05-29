"""PRD §10.5 — BOM Level 0~3 안정 + Level 4 coverage ≥ 60% 측정.

PRD §3.4 의 BOM 깊이 정의:
    Level 0: Manufacturer (OEM)
    Level 1: Vehicle Model
    Level 2: Trim/Year (variant)
    Level 3: System (powertrain / body / electrical 등 카테고리)
    Level 4: Module (예: motor-reducer, battery pack)
    Level 5~6: MVP 제외 (post-MVP).

본 메트릭이 측정하는 것:
    - L0~L3 entity 수 (안정 = 0 초과)
    - L4 module 수 (auto.components WHERE level=4)
    - **L4 coverage** = MVP OEM 의 VehicleModel 중 Level 4 module 매핑 ≥ 1 의 비율
      (Neo4j CONTAINS_COMPONENT / CONTAINS_SYSTEM 으로 측정).

사용:
    from eval.metrics.bom_coverage import collect_bom_coverage, format_summary_md
    print(format_summary_md(collect_bom_coverage()))
"""

from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger(__name__)


# PRD §10.5 — Level 4 coverage 목표.
TARGET_L4_COVERAGE = 0.60   # 60%

# MVP 5 OEM — Level 4 coverage 분모 계산용 (PRD §10.4 의 "OEM 5~8사" 충족).
MVP_OEM_NAMES = ("HYUNDAI", "KIA", "GENESIS", "TESLA", "FORD")


def collect_bom_coverage() -> dict[str, Any]:
    """L0~L4 BOM coverage 측정.

    Returns:
        {
          "levels": {
            "L0": {"name": "Manufacturer", "count": 4, "stable": True},
            "L1": {"name": "VehicleModel", "count": 58, "stable": True},
            ...
          },
          "l4_coverage": {
            "denominator": 58,    # MVP VehicleModel
            "with_module": 6,
            "ratio": 0.103,
            "target_ratio": 0.60,
            "target_met": False,
          },
          "l0_l3_stable": True,
          "section_met": False,
        }
    """
    out: dict[str, Any] = {
        "levels": {},
        "l4_coverage": {
            "denominator": 0, "with_module": 0, "ratio": 0.0,
            "target_ratio": TARGET_L4_COVERAGE, "target_met": False,
        },
        "l0_l3_stable": False,
        "section_met": False,
    }

    # PG 측 — Level 0~2 + Level 3/4 entity 수.
    try:
        import autonexusgraph.db.postgres as pg
        conn = pg.get_connection()
    except Exception as e:   # noqa: BLE001
        log.warning("[bom_coverage] PG 연결 실패: %s", e)
        return out

    with conn.cursor() as cur:
        # L0: distinct OEM brand 수 (variant 보유 한정).
        # vPIC 가 동일 OEM 을 여러 mfr_id 로 분할 보유 (예: Genesis Korea,
        # Genesis Motor LLC) — brand name 기준으로 합산.
        cur.execute("""
            SELECT COUNT(DISTINCT UPPER(m.name))
              FROM auto.master_manufacturers m
              JOIN auto.master_vehicle_models vm
                ON vm.manufacturer_id = m.manufacturer_id
              JOIN auto.master_vehicle_variants vv
                ON vv.model_id = vm.model_id
        """)
        l0_n = int(cur.fetchone()[0])

        # L1: Vehicle Model (variant 보유 한정 — MVP OEM 매칭).
        cur.execute("""
            SELECT COUNT(DISTINCT vm.model_id)
              FROM auto.master_vehicle_models vm
              JOIN auto.master_vehicle_variants vv
                ON vv.model_id = vm.model_id
        """)
        l1_n = int(cur.fetchone()[0])

        # L2: Variant.
        cur.execute("SELECT COUNT(*) FROM auto.master_vehicle_variants")
        l2_n = int(cur.fetchone()[0])

        # L3: System (distinct system_code from components).
        cur.execute("""
            SELECT COUNT(DISTINCT system_code) FROM auto.components
             WHERE system_code IS NOT NULL AND system_code != ''
        """)
        l3_n = int(cur.fetchone()[0])

        # L4: Module rows.
        cur.execute("SELECT COUNT(*) FROM auto.components WHERE level = 4")
        l4_n = int(cur.fetchone()[0])

    out["levels"] = {
        "L0": {"name": "Manufacturer", "count": l0_n, "stable": l0_n > 0},
        "L1": {"name": "VehicleModel", "count": l1_n, "stable": l1_n > 0},
        "L2": {"name": "VehicleVariant", "count": l2_n, "stable": l2_n > 0},
        "L3": {"name": "System", "count": l3_n, "stable": l3_n > 0},
        "L4": {"name": "Module", "count": l4_n, "stable": l4_n > 0},
    }
    out["l0_l3_stable"] = all(
        out["levels"][lv]["stable"] for lv in ("L0", "L1", "L2", "L3")
    )

    # L4 coverage — Neo4j 측에서 측정.
    # 매핑 경로 3가지 모두 합집합:
    #   (a) 직접: VehicleModel-[:CONTAINS_COMPONENT|CONTAINS_SYSTEM]->(Module|Component)
    #   (b) 리콜 hop: VehicleModel-[:HAS_VARIANT]->Variant-[:AFFECTED_BY]->Recall
    #                  -[:RECALL_OF]->(Module|Component)
    #   (c) 컴플레인 hop: VehicleModel-[:HAS_VARIANT]->Variant<-[:REPORTED_IN]-Complaint
    #                     -[:COMPLAINT_OF]->(Module|Component)
    try:
        from autonexusgraph.db.neo4j import get_driver
        driver = get_driver()
        with driver.session() as s:
            rec = s.run(
                """
                MATCH (mfr:Manufacturer)-[:MANUFACTURES]->(vm:VehicleModel)
                WHERE mfr.name IN $mvp
                OPTIONAL MATCH (vm)-[:CONTAINS_COMPONENT|CONTAINS_SYSTEM]->(c1)
                  WHERE (c1:Module OR c1:Component)
                OPTIONAL MATCH (vm)-[:HAS_VARIANT]->(:VehicleVariant)
                          -[:AFFECTED_BY]->(:Recall)-[:RECALL_OF]->(c2)
                  WHERE (c2:Module OR c2:Component)
                OPTIONAL MATCH (vm)-[:HAS_VARIANT]->(:VehicleVariant)
                          -[:REPORTED_IN]->(:Complaint)-[:COMPLAINT_OF]->(c3)
                  WHERE (c3:Module OR c3:Component)
                WITH vm, (c1 IS NOT NULL OR c2 IS NOT NULL OR c3 IS NOT NULL) AS has_l4
                RETURN count(DISTINCT vm) AS tot,
                       count(DISTINCT CASE WHEN has_l4 THEN vm END) AS withcomp
                """,
                mvp=list(MVP_OEM_NAMES),
            ).single()
            tot = int(rec["tot"] or 0)
            wc = int(rec["withcomp"] or 0)
    except Exception as e:   # noqa: BLE001
        log.warning("[bom_coverage] Neo4j L4 측정 실패: %s", e)
        tot = 0
        wc = 0

    ratio = (wc / tot) if tot else 0.0
    out["l4_coverage"] = {
        "denominator":  tot,
        "with_module":  wc,
        "ratio":        round(ratio, 4),
        "target_ratio": TARGET_L4_COVERAGE,
        "target_met":   ratio >= TARGET_L4_COVERAGE,
    }

    out["section_met"] = out["l0_l3_stable"] and out["l4_coverage"]["target_met"]
    return out


def format_summary_md(cov: dict[str, Any]) -> str:
    """`summary.md` §10.5 섹션."""
    lines = ["## BOM Level coverage (PRD §10.5)"]

    lv = cov.get("levels") or {}
    if not lv:
        lines.append("- (PG 미가용 또는 데이터 없음)")
        return "\n".join(lines)

    from ._format import mark

    lines.append("### Level 별 entity 수")
    lines.append("| Level | 의미 | 수 | 안정 |")
    lines.append("|---|---|---:|:---:|")
    for k in ("L0", "L1", "L2", "L3", "L4"):
        info = lv.get(k, {})
        lines.append(
            f"| {k} | {info.get('name', '?')} | {info.get('count', 0):,} "
            f"| {mark(info.get('stable', False))} |"
        )

    l4 = cov.get("l4_coverage", {})
    lines.append("")
    lines.append(
        f"- **L0~L3 안정** {mark(cov.get('l0_l3_stable', False))}"
    )
    lines.append(
        f"- **L4 coverage** = {l4.get('with_module', 0)}/{l4.get('denominator', 0)} "
        f"= **{l4.get('ratio', 0) * 100:.1f}%** "
        f"(목표 ≥ {l4.get('target_ratio', 0.6) * 100:.0f}%) "
        f"{mark(l4.get('target_met', False))}"
    )

    lines.append(
        f"\n**§10.5 종합** {mark(cov.get('section_met', False))}"
        + ("" if cov.get("section_met") else " — L4 coverage 추가 보강 필요 "
                                           "(공개 매뉴얼 + IR + 리콜 본문 LLM 추출)")
    )
    return "\n".join(lines)


__all__ = [
    "TARGET_L4_COVERAGE", "MVP_OEM_NAMES",
    "collect_bom_coverage", "format_summary_md",
]
