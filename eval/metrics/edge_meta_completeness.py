"""PRD §6.7 의무 메타 완전성 측정 (PRD §10.11 자동화).

모든 Neo4j 관계 타입이 의무 메타 (source_type / source_id / confidence_score /
validated_status / snapshot_year / extraction_method) 를 100% 채웠는지 검증.

PRD §10.11: "모든 SUPPLIED_BY 엣지에 confidence + provenance + snapshot_year
100% 채움" — 본 메트릭은 SUPPLIED_BY 포함 모든 main_hop / side_hop 관계로 확장.

사용:
    from eval.metrics.edge_meta_completeness import collect_edge_meta_completeness
    result = collect_edge_meta_completeness()
    print(format_summary_md(result))
"""

from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger(__name__)


# PRD §6.7 — 모든 엣지가 가져야 할 6개 의무 메타.
REQUIRED_META = (
    "source_type",
    "source_id",
    "confidence_score",
    "validated_status",
    "snapshot_year",
    "extraction_method",
)

# PRD §10.11 의 명시 대상. 다른 관계 타입도 동일 메타 강제.
PRD_REQUIRED_RELATIONS = ("SUPPLIED_BY",)


def collect_edge_meta_completeness() -> dict[str, Any]:
    """모든 관계 타입의 의무 메타 완전성 측정. 100% 미달 관계 강조.

    Returns:
        {
          "rels": {
            "SUPPLIED_BY": {
              "total": 30,
              "missing": {"source_id": 0, ...},
              "fully_compliant": True,
              "compliance_ratio": 1.0,
              "prd_required": True,
            },
            ...
          },
          "overall": {
            "total_edges": ...,
            "fully_compliant_rels": ...,
            "prd_required_compliant": True/False,
          }
        }
    """
    out: dict[str, Any] = {"rels": {}, "overall": {}}
    try:
        from autonexusgraph.db.neo4j import get_driver
    except Exception as e:   # noqa: BLE001
        log.warning("[edge_meta] Neo4j 모듈 import 실패: %s", e)
        return out

    try:
        driver = get_driver()
    except Exception as e:   # noqa: BLE001
        log.warning("[edge_meta] Neo4j 연결 실패: %s", e)
        return out

    # 1) 활성 관계 타입 목록.
    try:
        with driver.session() as session:
            rel_rows = session.run(
                "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType"
            ).data()
            rel_types = sorted(r["relationshipType"] for r in rel_rows)
    except Exception as e:   # noqa: BLE001
        log.warning("[edge_meta] relationshipTypes 호출 실패: %s", e)
        return out

    total_all = 0
    fully_count = 0
    prd_failed: list[str] = []

    for rt in rel_types:
        try:
            with driver.session() as session:
                # 동적 cypher 안전성 — rt 가 db.relationshipTypes 의 결과라 SAFE.
                clauses = ["count(r) AS total"]
                for m in REQUIRED_META:
                    clauses.append(f"count(r.{m}) AS with_{m}")
                cypher = f"MATCH ()-[r:`{rt}`]->() RETURN " + ", ".join(clauses)
                rec = session.run(cypher).single()
                if not rec:
                    continue
                total = int(rec["total"] or 0)
                if total == 0:
                    continue
                missing = {
                    m: total - int(rec[f"with_{m}"] or 0)
                    for m in REQUIRED_META
                }
                total_missing = sum(missing.values())
                full = total_missing == 0
                compliance = 1.0 - (total_missing / (total * len(REQUIRED_META)))
                out["rels"][rt] = {
                    "total": total,
                    "missing": missing,
                    "fully_compliant": full,
                    "compliance_ratio": round(compliance, 4),
                    "prd_required": rt in PRD_REQUIRED_RELATIONS,
                }
                total_all += total
                if full:
                    fully_count += 1
                elif rt in PRD_REQUIRED_RELATIONS:
                    prd_failed.append(rt)
        except Exception as e:   # noqa: BLE001
            log.warning("[edge_meta] %s 측정 실패: %s", rt, e)
            continue

    out["overall"] = {
        "total_edges":              total_all,
        "rel_types_measured":       len(out["rels"]),
        "fully_compliant_rel_types": fully_count,
        "prd_required_compliant":   len(prd_failed) == 0,
        "prd_failed":               prd_failed,
    }
    return out


def format_summary_md(quality: dict[str, Any]) -> str:
    """`summary.md` 의 §10.11 섹션."""
    lines: list[str] = ["## 엣지 의무 메타 완전성 (PRD §6.7 / §10.11)"]
    rels = quality.get("rels") or {}
    if not rels:
        lines.append("- (Neo4j 미가용 또는 관계 없음)")
        return "\n".join(lines)

    overall = quality.get("overall", {})
    n_total = overall.get("total_edges", 0)
    n_full = overall.get("fully_compliant_rel_types", 0)
    n_measured = overall.get("rel_types_measured", 0)
    prd_ok = overall.get("prd_required_compliant", False)
    met = "✅" if prd_ok else "❌"

    lines.append(
        f"- 측정 대상: **{n_measured}개 관계 타입**, **{n_total:,}개 엣지**"
    )
    lines.append(
        f"- 100% 의무 메타 충족 관계: **{n_full} / {n_measured}**"
    )
    lines.append(
        f"- **PRD §10.11 (SUPPLIED_BY 100%)** {met}"
        + (f" — fail: {overall.get('prd_failed') or []}" if not prd_ok else "")
    )

    # 관계 별 detail — total >= 5 인 것만 (작은 noise 제외).
    lines.append("")
    lines.append("### 관계별 상세")
    lines.append("| 관계 | edges | 완전성 | PRD 필수 | 누락 필드 |")
    lines.append("|---|---:|---:|:---:|---|")
    sorted_rels = sorted(rels.items(),
                          key=lambda kv: (-kv[1]["total"], kv[0]))
    for rt, info in sorted_rels:
        missing = info["missing"]
        missing_str = ", ".join(
            f"{k}({v})" for k, v in missing.items() if v > 0
        ) or "—"
        prd_mark = "🔵" if info["prd_required"] else "·"
        compliance_pct = info["compliance_ratio"] * 100
        comp_mark = "✅" if info["fully_compliant"] else "⚠️"
        lines.append(
            f"| {rt} | {info['total']:,} | {comp_mark} {compliance_pct:.1f}% "
            f"| {prd_mark} | {missing_str} |"
        )
    return "\n".join(lines)


__all__ = [
    "REQUIRED_META",
    "PRD_REQUIRED_RELATIONS",
    "collect_edge_meta_completeness",
    "format_summary_md",
]
