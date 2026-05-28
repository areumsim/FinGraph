#!/usr/bin/env python3
"""PRD §10 DoD 14 항목 종합 트래픽라이트 (B6).

각 항목별 측정 방식:
1. docker compose up                    — 본 스크립트로는 확인 불가 (런타임 외 의존). manifest 만.
2. Streamlit 도메인 토글 3종            — UI 수동 확인. manifest 만.
3. LLM Provider 환경변수 전환            — `LLM_PROVIDER` 환경 확인 + adapter 가용성 체크.
4. MVP 데이터 (OEM 5~8 × 모델 30~50)   — PG `auto.master_*` count.
5. BOM Level 0~3 안정 + L4 ≥ 60%       — `bom_coverage.py` 결과.
6. Bridge confidence ≥0.9 비율 80%+    — `eval/metrics/bridge_quality`.
7. Hybrid vs Vector +30%p              — 최신 eval/reports/*/manifest.json.
8. Cross-Domain QA L1~L4 목표          — gold_qa_cross_v0.jsonl 실행 결과.
9. 제원 Exact Match 95%+               — eval-auto summary.
10. Faithfulness 90%+                   — eval-auto summary.
11. SUPPLIED_BY 메타 100%               — `edge_meta_invariants.py`.
12. 코어 코드 변경 < 5%                  — `git diff --stat` 휴리스틱.
13. Main-Hop Efficiency 30% 감소         — `main_hop_efficiency` metric.
14. Latency 8s / 12s                    — `latency` metric.

본 audit 은 "측정 가능한 것만" 자동 측정하고 나머지는 명시적으로 'manual' 표기.
종료 코드:
    0: 항상 (리포트만 생성)
    1: --strict 면서 1 개 이상 ❌
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _latest_manifest() -> dict:
    rep = ROOT / "eval" / "reports"
    if not rep.exists():
        return {}
    cands = sorted([p for p in rep.iterdir() if p.is_dir()],
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for d in cands:
        m = d / "manifest.json"
        if m.exists():
            try:
                return json.loads(m.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
    return {}


def _mvp_master_counts() -> dict:
    try:
        from autograph.tools._db import query_one_dict
    except Exception:
        return {}
    out: dict = {}
    try:
        r = query_one_dict("SELECT COUNT(*) AS n FROM auto.master_manufacturers")
        out["manufacturers"] = int((r or {}).get("n", 0) or 0)
        r = query_one_dict("SELECT COUNT(*) AS n FROM auto.master_vehicle_models")
        out["models"] = int((r or {}).get("n", 0) or 0)
        r = query_one_dict("SELECT COUNT(*) AS n FROM auto.master_vehicle_variants")
        out["variants"] = int((r or {}).get("n", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        out["_error"] = str(exc)
    return out


def _core_diff_ratio() -> dict:
    """코어 (autonexusgraph/) 코드 라인 변경량 vs 전체 라인 — PRD §10.12."""
    import subprocess

    def _wc(path: Path) -> int:
        n = 0
        for f in path.rglob("*.py"):
            try:
                n += sum(1 for _ in f.open("r", encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
        return n

    try:
        diff = subprocess.check_output(
            ["git", "diff", "--stat", "HEAD~50..HEAD", "--",
             "src/autonexusgraph"],
            cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        diff = ""
    changed = 0
    for line in diff.splitlines():
        if "|" in line:
            try:
                num = line.split("|", 1)[1].strip().split()[0]
                changed += int(num)
            except Exception:  # noqa: BLE001
                pass
    total = _wc(ROOT / "src" / "autonexusgraph")
    return {
        "changed_lines": changed, "total_lines": total,
        "ratio": (changed / total) if total else 0.0,
    }


def collect() -> list[dict]:
    manifest = _latest_manifest()
    summary = manifest.get("summary", {})  # 없을 수 있음 — runner 는 어댑터별만 저장.
    bq = manifest.get("bridge_quality") or {}
    hvv = manifest.get("hybrid_vs_vector") or {}
    mhe = manifest.get("main_hop_efficiency") or {}
    lat = manifest.get("latency") or {}

    mvp = _mvp_master_counts()
    core = _core_diff_ratio()

    items: list[dict] = []

    def add(num: str, name: str, status: str, detail: str) -> None:
        items.append({"num": num, "name": name, "status": status, "detail": detail})

    add("1", "docker compose up", "manual", "런타임 외 의존 — 별도 확인")
    add("2", "Streamlit 도메인 토글 3종", "manual", "UI 수동 확인")
    add("3", "LLM Provider 환경변수 전환",
        "✅" if os.environ.get("LLM_PROVIDER") else "⚠",
        f"LLM_PROVIDER={os.environ.get('LLM_PROVIDER','<unset>')}")

    add("4", "MVP 범위 (OEM 5~8사, 모델 30~50)",
        "✅" if mvp.get("manufacturers", 0) >= 5 and mvp.get("models", 0) >= 30 else "❌",
        f"manufacturers={mvp.get('manufacturers','?')}, models={mvp.get('models','?')}, "
        f"variants={mvp.get('variants','?')}")

    # 5/11/13: bom_coverage 와 edge_meta 는 별도 audit 실행 필요 — manifest 에 없을 수 있음.
    # 본 audit 은 manifest 가 있으면 우선 사용, 없으면 'run audit' 안내.
    add("5", "BOM Level 0~3 안정, Level 4 ≥60%",
        "run-audit",
        "별도 `python scripts/audit/bom_coverage.py` 결과 참조")

    if bq.get("bridge"):
        ratio = bq["bridge"].get("high_confidence_ratio", 0.0)
        st = "✅" if ratio >= 0.8 else "❌"
        add("6", "Bridge confidence ≥0.9 비율 80%+",
            st, f"high_confidence_ratio={ratio:.1%}")
    else:
        add("6", "Bridge confidence ≥0.9 비율 80%+", "no-data",
            "eval 실행 시 bridge_quality 자동 수집 — DB 미가용일 수 있음")

    if hvv.get("available"):
        st = "✅" if hvv["target_met"] else "❌"
        add("7", "Hybrid vs Vector Multi-hop +30%p",
            st, f"em_diff_pp={hvv['em_diff_pp']}, f1_diff_pp={hvv['f1_diff_pp']}")
    else:
        add("7", "Hybrid vs Vector Multi-hop +30%p", "no-data",
            "vector + hybrid 어댑터 모두 multi-hop subset 필요")

    add("8", "Cross-Domain QA CD-L1~L4 목표",
        "run-cross",
        "gold_qa_cross_v0.jsonl 큐레이션 후 `eval/runners/run_qa_eval` 실행")

    # 9/10: summary 에서 추출.
    em = None
    faith = None
    for ad, s in (summary or {}).items():
        if ad == "hybrid":
            em = s.get("em")
            faith = s.get("faithfulness")
    add("9", "제원 Exact Match 95%+",
        ("✅" if em is not None and em >= 0.95 else ("❌" if em is not None else "no-data")),
        f"hybrid em={em}")
    add("10", "Faithfulness 90%+",
        ("✅" if faith is not None and faith >= 0.90 else ("❌" if faith is not None else "no-data")),
        f"hybrid faithfulness={faith}")

    add("11", "SUPPLIED_BY 메타 100%",
        "run-audit",
        "별도 `python scripts/audit/edge_meta_invariants.py --strict` 결과 참조")

    add("12", "코어 코드 변경 < 5%",
        ("✅" if core.get("ratio", 1.0) < 0.05 else "⚠"),
        f"changed_lines={core['changed_lines']}, total={core['total_lines']}, "
        f"ratio={core['ratio']:.1%}")

    if mhe.get("hybrid_vs_vector"):
        h = mhe["hybrid_vs_vector"]
        st = "✅" if h["target_met"] else "❌"
        add("13", "Main-Hop Efficiency 30% 감소",
            st, f"hybrid/vector ratio={h['ratio']}")
    else:
        add("13", "Main-Hop Efficiency 30% 감소", "no-data",
            "vector + hybrid 어댑터 같이 평가 필요")

    if lat:
        pass_rates = []
        for ad, s in lat.items():
            if not isinstance(s, dict):
                continue
            ip = s.get("target_internal_pass_rate")
            cp = s.get("target_cross_pass_rate")
            if ip is not None:
                pass_rates.append(("internal", ad, ip))
            if cp is not None:
                pass_rates.append(("cross", ad, cp))
        if pass_rates:
            best = max(r[2] for r in pass_rates)
            st = "✅" if best >= 0.9 else "⚠"
            add("14", "Latency 도메인내<8s / Cross<12s",
                st, "; ".join(f"{kind}/{ad}={rate:.1%}" for kind, ad, rate in pass_rates[:6]))
        else:
            add("14", "Latency 도메인내<8s / Cross<12s", "no-data", "")
    else:
        add("14", "Latency 도메인내<8s / Cross<12s", "no-data", "")

    return items


def render_md(items: list[dict]) -> str:
    lines = [f"# DoD Audit — {date.today().isoformat()}",
             "",
             "PRD §10 DoD 14 항목 트래픽라이트.",
             "",
             "| # | DoD | 상태 | 근거 |",
             "|---|---|---|---|"]
    for r in items:
        lines.append(f"| {r['num']} | {r['name']} | {r['status']} | {r['detail']} |")
    lines.append("")

    fails = [r for r in items if r["status"] == "❌"]
    if fails:
        lines.append("## ❌ 실패 항목")
        for r in fails:
            lines.append(f"- **{r['num']}. {r['name']}**: {r['detail']}")
    nodat = [r for r in items if r["status"] == "no-data"]
    if nodat:
        lines.append("")
        lines.append("## ⚠ 측정 데이터 부족")
        for r in nodat:
            lines.append(f"- **{r['num']}. {r['name']}**: {r['detail']}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--stdout", action="store_true")
    p.add_argument("--strict", action="store_true")
    args = p.parse_args()

    rows = collect()
    md = render_md(rows)
    if args.stdout:
        print(md)
    else:
        out = args.out or (ROOT / "data" / "reports" /
                           f"dod_audit_{date.today().strftime('%Y%m%d')}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"[dod_audit] wrote {out}")

    failed = sum(1 for r in rows if r["status"] == "❌")
    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
