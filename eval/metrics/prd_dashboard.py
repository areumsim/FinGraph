"""PRD §10 14개 success criteria 자동 측정 + 요약 대시보드.

상태 코드:
    ✅ 충족 / ❌ 미달 / ⚠️ 부분 / ⊘ 측정 불가 (LLM 또는 운영 데이터 필요)

CLI:
    python -m eval.metrics.prd_dashboard            # stdout 출력
    python -m eval.metrics.prd_dashboard --json     # JSON
    python -m eval.metrics.prd_dashboard -o path.md # 파일 저장
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any


log = logging.getLogger(__name__)


# 14개 success criteria.
CRITERIA = [
    ("10.1",  "AutoNexusGraph docker compose up", "infra"),
    ("10.2",  "Streamlit UI 도메인 토글 3종 동작", "infra"),
    ("10.3",  "LLM Provider 환경변수 전환", "config"),
    ("10.4",  "MVP 범위 (OEM 5~8 × 모델 30~50 × 2022~2024)", "data"),
    ("10.5",  "BOM Level 0~3 안정 + Level 4 coverage ≥ 60%", "data"),
    ("10.6",  "bridge.corp_entity QID/LEI 강매칭 confidence ≥0.9 비율 80%+", "bridge"),
    ("10.7",  "Hybrid vs Vector Multi-hop +30%p", "eval-llm"),
    ("10.8",  "Cross-Domain QA 4단계 (CD-L1 80%+/L2 70%+/L3 50%+/L4 40%+)", "eval-llm"),
    ("10.9",  "제원 수치 EM 95%+", "eval-llm"),
    ("10.10", "Faithfulness 90%+", "eval-llm"),
    ("10.11", "SUPPLIED_BY 엣지 confidence/provenance/snapshot_year 100%", "graph"),
    ("10.12", "AutoNexusGraph 코어 코드 변경 < 5%", "git"),
    ("10.13", "메인 홉 효율: 노드 탐색 -30%", "trace"),
    ("10.14", "평균 latency: 도메인 내 < 8s, Cross-Domain < 12s", "trace"),
]

# 각 카테고리 별 어떻게 처리되는지.
CATEGORY_HOW = {
    "infra":     "docker compose 실측 — 본 dashboard 는 미측정",
    "config":    "ENV 확인 — 본 dashboard 는 미측정",
    "data":      "자동 측정 가능",
    "bridge":    "자동 측정 가능",
    "graph":     "자동 측정 가능 (Neo4j)",
    "eval-llm":  "LLM_API_KEY 필요",
    "git":       "git diff 자동 계산 가능",
    "trace":     "운영 trace 필요",
}


def collect_dashboard() -> dict[str, Any]:
    """14개 criteria 측정 결과 한 곳에 집계."""
    out: dict[str, Any] = {"items": []}

    # 10.4 — data coverage.
    try:
        from eval.metrics.data_coverage import collect_data_coverage
        c10_4 = collect_data_coverage()
        oem_ok = c10_4.get("oem_target_met", False)
        model_ok = c10_4.get("model_target_met", False)
        year_ok = c10_4.get("year_coverage_target", False)
        if oem_ok and model_ok and year_ok:
            status, detail = "pass", f"OEM={c10_4['n_oems']} models={c10_4['n_models']} years={c10_4.get('year_range')}"
        else:
            partial = oem_ok or model_ok or year_ok
            status = "partial" if partial else "fail"
            misses = []
            if not oem_ok: misses.append(f"OEM={c10_4['n_oems']}<5")
            if not model_ok: misses.append(f"models={c10_4['n_models']}<30")
            if not year_ok: misses.append("year≠2022~2024")
            detail = f"{', '.join(misses)} (n_var={c10_4['n_variants']})"
    except Exception as e:   # noqa: BLE001
        log.warning("[dashboard] §10.4 측정 실패: %s", e)
        status, detail = "skip", f"err: {e}"
    out["items"].append({"id": "10.4", "status": status, "detail": detail})

    # 10.5 — BOM coverage.
    try:
        from eval.metrics.bom_coverage import collect_bom_coverage
        c10_5 = collect_bom_coverage()
        l0l3 = c10_5.get("l0_l3_stable", False)
        l4 = c10_5.get("l4_coverage") or {}
        l4_ok = l4.get("target_met", False)
        if l0l3 and l4_ok:
            status, detail = "pass", f"L0~L3 stable, L4={l4.get('ratio', 0) * 100:.1f}%"
        elif l0l3:
            status = "partial"
            detail = (f"L0~L3 ✅, L4={l4.get('with_module', 0)}/"
                      f"{l4.get('denominator', 0)} = {l4.get('ratio', 0) * 100:.1f}% < 60%")
        else:
            status, detail = "fail", "L0~L3 unstable"
    except Exception as e:   # noqa: BLE001
        log.warning("[dashboard] §10.5 측정 실패: %s", e)
        status, detail = "skip", f"err: {e}"
    out["items"].append({"id": "10.5", "status": status, "detail": detail})

    # 10.6 — bridge.
    try:
        from eval.metrics.bridge_quality import collect_bridge_quality
        bq = collect_bridge_quality()
        sm = (bq.get("bridge") or {}).get("strong_match") or {}
        if sm.get("target_met") is True:
            status = "pass"
        elif sm.get("total"):
            status = "fail"
        else:
            status = "skip"
        ratio = sm.get("high_confidence_ratio")
        detail = (f"strong_match {sm.get('high_confidence', 0)}/"
                  f"{sm.get('total', 0)} = "
                  + (f"{ratio * 100:.1f}%" if ratio is not None else "?"))
    except Exception as e:   # noqa: BLE001
        log.warning("[dashboard] §10.6 측정 실패: %s", e)
        status, detail = "skip", f"err: {e}"
    out["items"].append({"id": "10.6", "status": status, "detail": detail})

    # 10.11 — SUPPLIED_BY 메타 100%.
    try:
        from eval.metrics.edge_meta_completeness import collect_edge_meta_completeness
        em = collect_edge_meta_completeness()
        ok = em.get("overall", {}).get("prd_required_compliant", False)
        sb = (em.get("rels") or {}).get("SUPPLIED_BY") or {}
        n_total = sb.get("total", 0)
        if ok and n_total > 0:
            status, detail = "pass", f"SUPPLIED_BY {n_total} edges, 100% meta"
        elif n_total > 0:
            status, detail = "fail", f"SUPPLIED_BY {n_total} edges, miss={sb.get('missing')}"
        else:
            status, detail = "skip", "no SUPPLIED_BY edges"
    except Exception as e:   # noqa: BLE001
        log.warning("[dashboard] §10.11 측정 실패: %s", e)
        status, detail = "skip", f"err: {e}"
    out["items"].append({"id": "10.11", "status": status, "detail": detail})

    # 10.12 — 코어 코드 변경 < 5% (git diff baseline 자동 측정).
    try:
        from eval.metrics.core_diff import collect_core_diff
        cd = collect_core_diff()
        if not cd.get("available"):
            status, detail = "skip", "git 미가용 또는 baseline 미발견"
        else:
            pct = cd["change_ratio"] * 100
            base = (cd["baseline_commit"] or "")[:10]
            label = (f"baseline `{base}` → {cd['changed_loc']:,}/{cd['baseline_loc']:,} "
                     f"LOC = {pct:.2f}%")
            status = "pass" if cd["target_met"] else "fail"
            detail = label
    except Exception as e:   # noqa: BLE001
        log.warning("[dashboard] §10.12 측정 실패: %s", e)
        status, detail = "skip", f"err: {e}"
    out["items"].append({"id": "10.12", "status": status, "detail": detail})

    # LLM 의존 — ⊘.
    for cid in ("10.7", "10.8", "10.9", "10.10"):
        out["items"].append({
            "id": cid, "status": "blocked",
            "detail": "LLM_API_KEY 필요 — make eval-auto 실행 후 자동 측정",
        })

    # 인프라/설정.
    _CID_TO_CAT = {cid: cat for cid, _, cat in CRITERIA}
    for cid in ("10.1", "10.2", "10.3"):
        out["items"].append({
            "id": cid, "status": "n/a",
            "detail": CATEGORY_HOW[_CID_TO_CAT[cid]],
        })

    # 10.13, 10.14 — 운영 trace 필요.
    for cid in ("10.13", "10.14"):
        out["items"].append({
            "id": cid, "status": "blocked",
            "detail": "운영 trace 필요 (eval/runners 에서 latency·hop 수집 후 별도 메트릭)",
        })

    # 정렬.
    order = [c[0] for c in CRITERIA]
    out["items"].sort(key=lambda it: order.index(it["id"]) if it["id"] in order else 99)

    # 집계.
    counts = {"pass": 0, "fail": 0, "partial": 0, "blocked": 0, "n/a": 0, "skip": 0}
    for it in out["items"]:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    out["counts"] = counts
    out["measurable_total"] = sum(counts[k] for k in ("pass", "fail", "partial"))
    out["measurable_passed"] = counts["pass"]
    return out


_STATUS_MARK = {
    "pass":    "✅",
    "fail":    "❌",
    "partial": "⚠️",
    "blocked": "⊘",
    "n/a":     "·",
    "skip":    "?",
}


def format_summary_md(dash: dict[str, Any]) -> str:
    lines = ["# PRD §10 Success Criteria — Dashboard"]
    items = dash.get("items") or []

    counts = dash.get("counts", {})
    n_mes = dash.get("measurable_total", 0)
    n_pass = dash.get("measurable_passed", 0)

    lines.append(
        f"\n**측정 가능 항목**: {n_pass} pass / {n_mes} measurable "
        f"(⊘ {counts.get('blocked', 0)} LLM 필요, "
        f"· {counts.get('n/a', 0)} 외부 측정, "
        f"⚠️ {counts.get('partial', 0)} 부분, ❌ {counts.get('fail', 0)} 미달)"
    )

    lines.append("\n| ID | 기준 | 상태 | 상세 |")
    lines.append("|---|---|:---:|---|")
    title_map = {cid: title for cid, title, _ in CRITERIA}
    for it in items:
        cid = it["id"]
        mark = _STATUS_MARK.get(it["status"], "?")
        lines.append(
            f"| §{cid} | {title_map.get(cid, '?')} | {mark} | {it['detail']} |"
        )

    lines.append("\n## 범례")
    lines.append("- ✅ 자동 측정으로 통과")
    lines.append("- ❌ 자동 측정으로 미달")
    lines.append("- ⚠️ 부분 충족")
    lines.append("- ⊘ LLM_API_KEY 또는 운영 trace 필요 (본 dashboard 범위 밖)")
    lines.append("- · 외부 측정 (docker / git / ENV)")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(prog="eval.metrics.prd_dashboard")
    ap.add_argument("-o", "--out", help="md 저장 경로 (생략 시 stdout)")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level)

    dash = collect_dashboard()
    if args.json:
        text = json.dumps(dash, ensure_ascii=False, indent=2)
    else:
        text = format_summary_md(dash)

    if args.out:
        from pathlib import Path
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        log.info("저장: %s", args.out)
    else:
        print(text)


if __name__ == "__main__":
    main()


__all__ = ["CRITERIA", "collect_dashboard", "format_summary_md"]
