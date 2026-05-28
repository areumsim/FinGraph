#!/usr/bin/env python3
"""BOM Level 0~5 coverage 측정 (PRD §10 DoD #5).

기준: "Level 0~3 안정, Level 4 coverage ≥ 60%".

측정:
- Level 0: Manufacturer 노드 수
- Level 1: VehicleModel 노드 수 + (Manufacturer)-[:MANUFACTURES] 보유율
- Level 2: VehicleVariant 노드 수 + (VehicleModel)-[:HAS_VARIANT] 보유율
- Level 3: System 노드 수 + (VehicleModel)-[:CONTAINS_SYSTEM] 보유율 (variant 단위 평균)
- Level 4: Module 노드 수 + variant 단위 Level 4 coverage = (variants with ≥1 Module 노드)/total
- Level 5: Part 노드 수 (post-MVP — 정보만)
- SUPPLIED_BY: Module/Part → Supplier 엣지 수 + provenance 분포

출력: data/reports/bom_coverage_<date>.md 또는 stdout (--stdout).

DB 미가용 시 exit 1.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


_QUERIES: list[tuple[str, str]] = [
    ("manufacturer_count",
     "MATCH (m:Manufacturer) RETURN count(m) AS n"),
    ("model_count",
     "MATCH (m:VehicleModel) RETURN count(m) AS n"),
    ("model_with_manufactures",
     "MATCH (:Manufacturer)-[:MANUFACTURES]->(m:VehicleModel) RETURN count(DISTINCT m) AS n"),
    ("variant_count",
     "MATCH (v:VehicleVariant) RETURN count(v) AS n"),
    ("variant_with_has_variant",
     "MATCH (:VehicleModel)-[:HAS_VARIANT]->(v:VehicleVariant) RETURN count(DISTINCT v) AS n"),
    ("system_count",
     "MATCH (s:System) RETURN count(s) AS n"),
    ("model_with_contains_system",
     "MATCH (m:VehicleModel)-[:CONTAINS_SYSTEM]->(:System) RETURN count(DISTINCT m) AS n"),
    ("module_count",
     "MATCH (m:Module) RETURN count(m) AS n"),
    # Level 4 coverage 정의: 자신이 직접 Module 을 가진 또는 model 의 module 를 통해
    # 간접 연결된 variant 비율.
    ("variant_with_module_l4",
     """
     MATCH (v:VehicleVariant)
     OPTIONAL MATCH (v)<-[:HAS_VARIANT]-(m:VehicleModel)-[:CONTAINS_COMPONENT]->(mod:Module)
     OPTIONAL MATCH (v)-[:CONTAINS_COMPONENT]->(mod2:Module)
     WITH v, count(DISTINCT mod) + count(DISTINCT mod2) AS modules
     RETURN count(CASE WHEN modules > 0 THEN 1 END) AS n_with, count(v) AS n_total
     """),
    ("part_count",
     "MATCH (p:Part) RETURN count(p) AS n"),
    ("supplier_count",
     "MATCH (s:Supplier) RETURN count(s) AS n"),
    ("supplied_by_count",
     "MATCH ()-[r:SUPPLIED_BY]->(:Supplier) RETURN count(r) AS n"),
    ("supplied_by_by_provenance",
     """
     MATCH ()-[r:SUPPLIED_BY]->()
     RETURN coalesce(r.source_type, '<missing>') AS source_type, count(r) AS n
     ORDER BY n DESC
     """),
    ("manufactured_at_count",
     "MATCH (:VehicleModel)-[r:MANUFACTURED_AT]->(:Plant) RETURN count(r) AS n"),
    ("owns_plant_count",
     "MATCH (:Manufacturer)-[r:OWNS_PLANT]->(:Plant) RETURN count(r) AS n"),
    ("complies_with_count",
     "MATCH (:VehicleVariant)-[r:COMPLIES_WITH]->(:Standard) RETURN count(r) AS n"),
    ("safety_rated_by_count",
     "MATCH (:VehicleVariant)-[r:SAFETY_RATED_BY]->(:Standard) RETURN count(r) AS n"),
    ("affected_by_count",
     "MATCH (:VehicleVariant)-[r:AFFECTED_BY]->(:Recall) RETURN count(r) AS n"),
    ("recall_of_count",
     "MATCH (:Recall)-[r:RECALL_OF]->() RETURN count(r) AS n"),
]


def _run_query(session, key: str, cypher: str) -> dict:
    try:
        rec = list(session.run(cypher))
    except Exception as exc:  # noqa: BLE001
        return {"_error": str(exc)}
    if not rec:
        return {}
    if len(rec) == 1:
        return dict(rec[0])
    # 다행 결과 — 첫 row 의 key 를 그대로 list of dict.
    return {"rows": [dict(r) for r in rec]}


def collect() -> dict:
    from autonexusgraph.db.neo4j import get_driver
    out: dict = {}
    driver = get_driver()
    with driver.session() as session:
        for key, cypher in _QUERIES:
            out[key] = _run_query(session, key, cypher)
    return out


def render_md(stats: dict) -> str:
    lines = [f"# BOM Coverage Audit — {date.today().isoformat()}",
             "",
             "기준 (PRD §10 DoD #5): **Level 0~3 안정, Level 4 coverage ≥ 60%**",
             ""]

    def get(key: str, sub: str = "n") -> int:
        v = stats.get(key) or {}
        try:
            return int(v.get(sub, 0))
        except (TypeError, ValueError):
            return 0

    mfr = get("manufacturer_count")
    model = get("model_count")
    model_m = get("model_with_manufactures")
    var = get("variant_count")
    var_h = get("variant_with_has_variant")
    sys_ = get("system_count")
    model_cs = get("model_with_contains_system")
    mod = get("module_count")
    part = get("part_count")
    sup = get("supplier_count")
    sb = get("supplied_by_count")
    ma = get("manufactured_at_count")
    op = get("owns_plant_count")
    cw = get("complies_with_count")
    sr = get("safety_rated_by_count")

    var_with_l4 = (stats.get("variant_with_module_l4") or {})
    n_with = int(var_with_l4.get("n_with", 0) or 0)
    n_total = int(var_with_l4.get("n_total", 0) or 0)
    l4_ratio = (n_with / n_total) if n_total else 0.0
    l4_met = "✅" if l4_ratio >= 0.60 else "❌"

    lines.append("## Level 별 노드 / 엣지 카운트")
    lines.append("| Level | 라벨 | 노드 수 | 메인 홉 진입 | 비고 |")
    lines.append("|---|---|---|---|---|")
    lines.append(f"| 0 | Manufacturer | {mfr} | — | DoD #4 (MVP OEM 5~8사) |")
    lines.append(f"| 1 | VehicleModel | {model} | {model_m} ([:MANUFACTURES]) | |")
    lines.append(f"| 2 | VehicleVariant | {var} | {var_h} ([:HAS_VARIANT]) | |")
    lines.append(f"| 3 | System | {sys_} | {model_cs} (model with CONTAINS_SYSTEM) | |")
    lines.append(f"| 4 | Module | {mod} | {n_with}/{n_total} variants | coverage **{l4_ratio:.1%}** {l4_met} (목표 60%+) |")
    lines.append(f"| 5 | Part | {part} | — | post-MVP (PRD §3.4) |")
    lines.append("")

    lines.append("## 보조·횡단 엣지")
    lines.append(f"- Supplier 노드: **{sup}**, SUPPLIED_BY 엣지: **{sb}**")
    lines.append(f"- MANUFACTURED_AT (모델↔공장): **{ma}**, OWNS_PLANT: {op}")
    lines.append(f"- COMPLIES_WITH (차량↔표준): **{cw}**, SAFETY_RATED_BY: {sr}")
    lines.append(f"- AFFECTED_BY: {get('affected_by_count')}, "
                 f"RECALL_OF: {get('recall_of_count')}")
    lines.append("")

    sb_prov = (stats.get("supplied_by_by_provenance") or {}).get("rows") or []
    if sb_prov:
        lines.append("### SUPPLIED_BY provenance 분포")
        for r in sb_prov:
            lines.append(f"- {r.get('source_type','<missing>')}: {r.get('n',0)}")
        lines.append("")

    # DoD 트래픽라이트.
    lines.append("## DoD 트래픽라이트")
    rows = [
        ("L0 안정", "✅" if mfr >= 5 else "⚠"),
        ("L1 안정", "✅" if model >= 30 else "⚠"),
        ("L2 안정", "✅" if var >= 100 else "⚠"),
        ("L3 안정", "✅" if sys_ >= 19 else "⚠"),
        ("L4 ≥60%", l4_met),
        ("Supplier 엣지 메타", "✅" if sb > 0 else "⚠"),
        ("OEM↔공장", "✅" if ma > 0 else "⚠ (MANUFACTURED_AT seed 미적재)"),
    ]
    for k, v in rows:
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=None,
                   help="출력 .md 경로. 기본 data/reports/bom_coverage_<date>.md")
    p.add_argument("--stdout", action="store_true",
                   help="파일 대신 stdout 으로만 출력")
    args = p.parse_args()

    try:
        stats = collect()
    except Exception as exc:  # noqa: BLE001
        print(f"[bom_coverage] Neo4j 연결/쿼리 실패: {exc}", file=sys.stderr)
        return 1

    md = render_md(stats)
    if args.stdout:
        print(md)
        return 0

    out = args.out or (ROOT / "data" / "reports" /
                       f"bom_coverage_{date.today().strftime('%Y%m%d')}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"[bom_coverage] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
