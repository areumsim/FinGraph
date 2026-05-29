"""PRD §10.4 — MVP 데이터 적재 범위 자동 측정.

§10.4: "MVP 범위 (OEM 5~8사 × 모델 30~50종 × 2022~2024 연식) 데이터 3저장소 적재".

본 메트릭은 PG 측 row count 만 검증. Neo4j/Vector 측은 별도 모듈.

사용:
    from eval.metrics.data_coverage import collect_data_coverage, format_summary_md
    print(format_summary_md(collect_data_coverage()))
"""

from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger(__name__)


# PRD §10.4 의 목표값 (MVP 1차).
TARGET_OEM_RANGE = (5, 8)
TARGET_MODEL_RANGE = (30, 50)
TARGET_YEAR_RANGE = (2022, 2024)


def collect_data_coverage() -> dict[str, Any]:
    """MVP 데이터 적재 범위 측정.

    Returns:
        {
          "oems_with_variants": [{"name": "HYUNDAI", "models": 20, "variants": 75}, ...],
          "n_oems": 4,
          "n_models": 58,
          "n_variants": 222,
          "year_range": (2020, 2024),
          "year_dist": {2020: 41, ..., 2024: 48},
          "year_coverage_target": True/False,
          "oem_target_met": False (4 < 5),
          "model_target_met": True (58 ≥ 30),
          "events": {"recalls": 219, "complaints": 16005, ...},
          "spec_measurements": 3329,
          "components": 44,
        }
    """
    out: dict[str, Any] = {
        "oems_with_variants": [],
        "n_oems": 0,
        "n_models": 0,
        "n_variants": 0,
        "year_range": None,
        "year_dist": {},
        "year_coverage_target": False,
        "oem_target_met": False,
        "model_target_met": False,
        "events": {},
        "spec_measurements": 0,
        "components": 0,
    }

    try:
        import autonexusgraph.db.postgres as pg
        conn = pg.get_connection()
    except Exception as e:   # noqa: BLE001
        log.warning("[data_coverage] PG 연결 실패: %s", e)
        return out

    with conn.cursor() as cur:
        # 1) OEM × model × variant.
        cur.execute("""
            SELECT m.name, COUNT(DISTINCT vm.model_id) AS models,
                          COUNT(DISTINCT vv.variant_id) AS variants
              FROM auto.master_manufacturers m
              LEFT JOIN auto.master_vehicle_models vm
                ON vm.manufacturer_id = m.manufacturer_id
              LEFT JOIN auto.master_vehicle_variants vv
                ON vv.model_id = vm.model_id
             WHERE vv.variant_id IS NOT NULL
             GROUP BY m.name
             ORDER BY variants DESC
        """)
        rows = cur.fetchall()
        out["oems_with_variants"] = [
            {"name": r[0], "models": int(r[1]), "variants": int(r[2])}
            for r in rows
        ]
        out["n_oems"] = len(out["oems_with_variants"])
        out["n_models"] = sum(o["models"] for o in out["oems_with_variants"])
        out["n_variants"] = sum(o["variants"] for o in out["oems_with_variants"])

        # 2) 연식 분포.
        cur.execute("""
            SELECT model_year, COUNT(*) FROM auto.master_vehicle_variants
             WHERE model_year IS NOT NULL
             GROUP BY model_year ORDER BY model_year
        """)
        ydist = {int(r[0]): int(r[1]) for r in cur.fetchall()}
        out["year_dist"] = ydist
        if ydist:
            out["year_range"] = (min(ydist), max(ydist))
            # 목표 (2022~2024) 전체 포함 여부.
            out["year_coverage_target"] = all(
                y in ydist for y in range(TARGET_YEAR_RANGE[0],
                                            TARGET_YEAR_RANGE[1] + 1)
            )

        # 3) 이벤트.
        for src_key, tbl in (
            ("recalls", "auto.events_recalls"),
            ("complaints", "auto.events_complaints"),
            ("investigations", "auto.events_investigations"),
        ):
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            out["events"][src_key] = int(cur.fetchone()[0])
        # safety_ratings 는 별도 — spec_measurements 안에 들어감.
        cur.execute("""
            SELECT COUNT(*) FROM auto.spec_measurements
             WHERE measure_key LIKE 'safety.%'
        """)
        out["events"]["safety_ratings"] = int(cur.fetchone()[0])

        # 4) spec / components.
        cur.execute("SELECT COUNT(*) FROM auto.spec_measurements")
        out["spec_measurements"] = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM auto.components")
        out["components"] = int(cur.fetchone()[0])

        # 5) SEC 재무.
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT sec_cik) FROM auto.oem_financials_sec")
        rr = cur.fetchone()
        out["sec_financials"] = {
            "rows": int(rr[0]), "ciks": int(rr[1]),
        }

    # 하한 충족 = ✅ (상한 초과는 더 풍부한 데이터로 간주).
    out["oem_target_met"] = out["n_oems"] >= TARGET_OEM_RANGE[0]
    out["model_target_met"] = out["n_models"] >= TARGET_MODEL_RANGE[0]

    return out


def format_summary_md(cov: dict[str, Any]) -> str:
    """`summary.md` 의 §10.4 섹션."""
    lines = ["## 데이터 적재 범위 (PRD §10.4)"]

    if not cov.get("oems_with_variants"):
        lines.append("- (PG 미가용 또는 데이터 없음)")
        return "\n".join(lines)

    n_oem = cov["n_oems"]
    n_model = cov["n_models"]
    n_var = cov["n_variants"]
    yr = cov.get("year_range")
    oem_ok = cov["oem_target_met"]
    model_ok = cov["model_target_met"]
    year_ok = cov["year_coverage_target"]

    from ._format import mark

    lines.append(
        f"- OEM: **{n_oem}** (목표 {TARGET_OEM_RANGE[0]}~{TARGET_OEM_RANGE[1]}) {mark(oem_ok)}"
    )
    lines.append(
        f"- 모델: **{n_model}** (목표 {TARGET_MODEL_RANGE[0]}~{TARGET_MODEL_RANGE[1]}) {mark(model_ok)}"
    )
    lines.append(
        f"- 변형 (variants): **{n_var}** | 연식 {yr[0]}~{yr[1]} {mark(year_ok)} "
        f"(목표 {TARGET_YEAR_RANGE[0]}~{TARGET_YEAR_RANGE[1]} 포함)"
    )

    lines.append("\n### OEM 별 (variants 보유)")
    lines.append("| OEM | models | variants |")
    lines.append("|---|---:|---:|")
    for o in cov["oems_with_variants"]:
        lines.append(f"| {o['name']} | {o['models']:,} | {o['variants']:,} |")

    lines.append("\n### 연식 분포 (variants)")
    lines.append("| year | n |")
    lines.append("|---|---:|")
    for y in sorted(cov["year_dist"]):
        lines.append(f"| {y} | {cov['year_dist'][y]:,} |")

    ev = cov.get("events") or {}
    lines.append("\n### 이벤트·제원·재무")
    lines.append(f"- recalls: {ev.get('recalls', 0):,}")
    lines.append(f"- complaints: {ev.get('complaints', 0):,}")
    lines.append(f"- investigations: {ev.get('investigations', 0):,}")
    lines.append(f"- safety_ratings (spec_measurements): {ev.get('safety_ratings', 0):,}")
    lines.append(f"- spec_measurements (total): {cov['spec_measurements']:,}")
    lines.append(f"- components: {cov['components']:,}")
    sec = cov.get("sec_financials") or {}
    lines.append(
        f"- SEC OEM financials: {sec.get('rows', 0):,} rows / "
        f"{sec.get('ciks', 0)} CIK"
    )

    overall_ok = oem_ok and model_ok and year_ok
    lines.append(
        f"\n**§10.4 종합** {mark(overall_ok)} "
        + ("— MVP 범위 충족" if overall_ok
           else "— 미달 항목 있음 (PRD 목표 vs MVP 실측 차이)")
    )
    return "\n".join(lines)


__all__ = [
    "TARGET_OEM_RANGE", "TARGET_MODEL_RANGE", "TARGET_YEAR_RANGE",
    "collect_data_coverage", "format_summary_md",
]
