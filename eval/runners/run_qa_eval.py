#!/usr/bin/env python3
"""QA 평가 runner — 4 시스템 × gold set 매트릭스 실행.

사용:
    python -m eval.runners.run_qa_eval --gold eval/qa_gold/gold_qa_v0.jsonl \\
        --adapters vector,graph,hybrid,sql_vec \\
        --run-id baseline_$(date +%Y%m%d_%H%M%S)

옵션:
    --gold PATH           : qa_gold jsonl
    --adapters CSV        : vector,graph,hybrid,sql_vec 중 선택
    --top-k N             : Hits@k k 값 (기본 5)
    --limit N             : 처음 N row 만 (smoke test)
    --max-llm-calls N     : 어댑터별 LLM 호출 상한 (CostBudget)
    --max-llm-tokens N    : 어댑터별 토큰 상한
    --max-cost-usd FLOAT  : 어댑터별 USD 한도 (cost_tracker 와 통합)
    --enable-judge        : llm_judge 활성화 (별도 LLM 비용 발생 — 비용 가드 통과 필요)
    --run-id ID           : 출력 디렉토리 이름 (없으면 timestamp 자동)
    --resume              : 이미 처리된 qid 는 skip (기본 True)
    --force               : resume 무시하고 재실행

산출:
    eval/reports/<run-id>/
      <adapter>_predictions.jsonl    : 어댑터별 row 응답 (raw)
      per_question.csv               : 모든 어댑터 × 모든 qid metric
      summary.md                     : 어댑터별 평균 + 통계
      manifest.json                  : 환경·git·budget 메타
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


# ─── IO helpers ─────────────────────────────────────────────
def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def git_info() -> dict[str, str]:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        sha = ""
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True,
            stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        branch = ""
    try:
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], text=True,
            stderr=subprocess.DEVNULL
        ).strip())
    except Exception:
        dirty = False
    return {"sha": sha, "branch": branch, "dirty": str(dirty)}


# ─── Budget (per-adapter — 어댑터마다 0 부터 시작) ─────────────
@dataclass
class CostBudget:
    """평가 루프 LLM 호출/토큰/USD 상한.

    어댑터 child 가 호출하는 LLM 비용은 AgentResponse.cost_usd / tokens_used 로
    노출. 부모(러너)는 매 row 후 누적해 상한 초과 시 break.

    상한이 None 이면 비활성. 모든 비용 가드 (memory: feedback-llm-cost-brake) 와
    독립 — 어댑터 내부 BudgetAwareLLMClient 가 hard_limit 도 동시 적용.
    """

    max_calls:  int | None = None
    max_tokens: int | None = None
    max_cost:   float | None = None
    calls:  int   = 0
    tokens: int   = 0
    cost:   float = 0.0
    halted: bool  = False
    halt_reason: str = ""

    def record(self, *, tokens_used: int = 0, cost_usd: float = 0.0) -> None:
        self.calls += 1
        try:
            self.tokens += int(tokens_used or 0)
        except (TypeError, ValueError):
            pass
        try:
            self.cost += float(cost_usd or 0.0)
        except (TypeError, ValueError):
            pass

    def check_limit(self) -> str:
        if self.max_calls is not None and self.calls >= self.max_calls:
            return f"max_calls 도달 ({self.calls}/{self.max_calls})"
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            return f"max_tokens 도달 ({self.tokens}/{self.max_tokens})"
        if self.max_cost is not None and self.cost >= self.max_cost:
            return f"max_cost_usd 도달 (${self.cost:.4f}/${self.max_cost:.4f})"
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost,
            "calls": self.calls,
            "tokens": self.tokens,
            "cost_usd": round(self.cost, 6),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }


# ─── adapter 실행 (resume 지원) ─────────────────────────────
def run_adapter_on_gold(
    adapter,
    gold_rows: list[dict],
    out_path: Path,
    budget: CostBudget | None = None,
) -> list[dict]:
    """row 별로 adapter.query() → out_path 에 실시간 append. 이미 처리된 qid skip."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                done.add(json.loads(line)["qid"])
            except Exception:
                pass

    total = len(gold_rows)
    for i, row in enumerate(gold_rows, 1):
        qid = row["qid"]
        if qid in done:
            continue
        if budget is not None and budget.halted:
            break

        print(f"  [{adapter.name}] {i}/{total} qid={qid}", flush=True)
        from eval.adapters.base import AgentResponse
        try:
            resp = adapter.query(row["question"], domain=row.get("domain"))
        except Exception as exc:  # noqa: BLE001
            resp = AgentResponse(
                refused=True,
                refusal_reason=f"exception:{type(exc).__name__}:{exc}",
            )

        rec = {
            "qid": qid,
            "adapter": adapter.name,
            "answer": resp.answer,
            "refused": resp.refused,
            "refusal_reason": resp.refusal_reason,
            "answer_entities": resp.answer_entities,
            "evidence": [dataclasses.asdict(e) for e in resp.evidence],
            "cypher": resp.cypher,
            "question_kind": resp.question_kind,
            "answer_confidence": resp.answer_confidence,
            "data_completeness": resp.data_completeness,
            "latency_sec": resp.latency_sec,
            "cost_usd": resp.cost_usd,
            "tokens_used": resp.tokens_used,
            "diagnostics": resp.diagnostics,
            "raw": resp.raw,
        }
        append_jsonl(out_path, rec)

        if budget is not None:
            budget.record(tokens_used=resp.tokens_used, cost_usd=resp.cost_usd)
            reason = budget.check_limit()
            if reason:
                budget.halted = True
                budget.halt_reason = reason
                print(f"  [budget] 중단: {reason}", file=sys.stderr, flush=True)
                break

    return load_jsonl(out_path)


# ─── metric 산정 ────────────────────────────────────────────
def compute_per_question_metrics(
    gold_rows: list[dict],
    pred_rows: list[dict],
    *,
    top_k: int = 5,
    enable_judge: bool = False,
) -> list[dict]:
    """gold × pred 머지 → per-question metric dict."""
    from eval.metrics import (
        exact_match, token_f1, hits_at_k, faithfulness, llm_judge,
    )

    by_qid = {p["qid"]: p for p in pred_rows}
    out: list[dict] = []

    for g in gold_rows:
        qid = g["qid"]
        p = by_qid.get(qid)
        if not p:
            continue

        golds_text = g.get("gold_answer_text") or []
        if isinstance(golds_text, str):
            golds_text = [golds_text]
        gold_entities = g.get("gold_answer_entities") or []
        ev_texts = [e.get("evidence_text", "") for e in (p.get("evidence") or [])]
        is_answerable = bool(g.get("is_answerable", True))

        m = {
            "qid": qid,
            "adapter": p.get("adapter", ""),
            "scenario_id": g.get("scenario_id", ""),
            "question_kind": p.get("question_kind", ""),
            "complexity": g.get("complexity", ""),
            "requires_multi_hop": bool(g.get("requires_multi_hop")),

            "em":           exact_match(p.get("answer", ""), golds_text),
            "f1":           token_f1(p.get("answer", ""), golds_text),
            "hits@k":       hits_at_k(p.get("answer_entities") or [], gold_entities, k=top_k),
            "faithfulness": faithfulness(p.get("answer", ""), ev_texts),

            "refused":      bool(p.get("refused")),
            "is_answerable": is_answerable,

            "latency_sec":  float(p.get("latency_sec") or 0.0),
            "cost_usd":     float(p.get("cost_usd") or 0.0),
            "tokens_used":  int(p.get("tokens_used") or 0),
        }

        # LLM judge — 비용 가드 통과한 호출만. enable=False 면 None.
        if enable_judge:
            j = llm_judge(g["question"], p.get("answer", ""),
                          (golds_text or [""])[0], enable=True)
            if j:
                m["judge_correctness"]  = j.get("correctness")
                m["judge_completeness"] = j.get("completeness")
                m["judge_fluency"]      = j.get("fluency")
        out.append(m)
    return out


def _safe_mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def summarize_by_adapter(per_q: list[dict]) -> dict[str, dict]:
    """어댑터별 평균 metric + 표본수."""
    from eval.metrics.refusal import refusal_metrics

    out: dict[str, dict] = {}
    by_adapter: dict[str, list[dict]] = {}
    for r in per_q:
        by_adapter.setdefault(r["adapter"], []).append(r)

    for adapter, rows in by_adapter.items():
        ref = refusal_metrics(rows)
        out[adapter] = {
            "n":                len(rows),
            "em":               _safe_mean([r["em"] for r in rows]),
            "f1":               _safe_mean([r["f1"] for r in rows]),
            "hits@k":           _safe_mean([r["hits@k"] for r in rows]),
            "faithfulness":     _safe_mean([r["faithfulness"] for r in rows]),
            "latency_sec_avg":  _safe_mean([r["latency_sec"] for r in rows]),
            "cost_usd_total":   sum(r["cost_usd"] for r in rows),
            "tokens_total":     sum(r["tokens_used"] for r in rows),
            **{f"refusal_{k}": v for k, v in ref.items()},
        }
        # multi-hop subset
        mh = [r for r in rows if r["requires_multi_hop"]]
        if mh:
            out[adapter]["multi_hop_n"] = len(mh)
            out[adapter]["multi_hop_em"] = _safe_mean([r["em"] for r in mh])
            out[adapter]["multi_hop_f1"] = _safe_mean([r["f1"] for r in mh])
    return out


# ─── multi-hop 어댑터 간 차이 (PRD §10.7 — hybrid vs vector +30%p 자동 측정) ────
def compute_hybrid_vs_vector(summary: dict[str, dict]) -> dict[str, Any]:
    """hybrid 어댑터가 vector 어댑터 대비 multi-hop EM/F1 에서 얼마나 우위인지.

    summary 에 두 어댑터 모두 ``multi_hop_n`` 가 있을 때만 의미. 단위는 %p (퍼센트 포인트).
    PRD §10.7 목표: **+30%p**.
    """
    out: dict[str, Any] = {
        "available": False,
        "target_diff_pp": 30.0,
        "target_met": False,
    }
    h = summary.get("hybrid") or {}
    v = summary.get("vector") or {}
    if "multi_hop_n" not in h or "multi_hop_n" not in v:
        return out

    em_diff = (h["multi_hop_em"] - v["multi_hop_em"]) * 100.0
    f1_diff = (h["multi_hop_f1"] - v["multi_hop_f1"]) * 100.0
    out.update({
        "available": True,
        "multi_hop_n":          {"hybrid": h["multi_hop_n"], "vector": v["multi_hop_n"]},
        "hybrid_em":            h["multi_hop_em"],
        "vector_em":            v["multi_hop_em"],
        "em_diff_pp":           round(em_diff, 2),
        "hybrid_f1":            h["multi_hop_f1"],
        "vector_f1":            v["multi_hop_f1"],
        "f1_diff_pp":           round(f1_diff, 2),
        # 둘 중 하나라도 30%p 이상이면 PRD 목표 met.
        "target_met":           (em_diff >= 30.0) or (f1_diff >= 30.0),
    })
    return out


# ─── 보고서 출력 ────────────────────────────────────────────
def write_summary_md(summary: dict, manifest: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append(f"# FinGraph QA Evaluation — {manifest.get('run_id', '')}")
    lines.append("")
    lines.append(f"- gold: `{manifest.get('gold', '')}`")
    lines.append(f"- git:  `{manifest['git']['sha'][:10]}` ({manifest['git']['branch']}, dirty={manifest['git']['dirty']})")
    lines.append(f"- when: {manifest.get('started_at', '')}")
    lines.append("")

    lines.append("## 어댑터별 요약")
    headers = ["adapter", "n", "em", "f1", "hits@k", "faithfulness",
               "latency", "cost_usd", "tokens", "refused", "false_refusal"]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for adapter, s in summary.items():
        lines.append("| " + " | ".join([
            adapter,
            str(s["n"]),
            f"{s['em']:.3f}",
            f"{s['f1']:.3f}",
            f"{s['hits@k']:.3f}",
            f"{s['faithfulness']:.3f}",
            f"{s['latency_sec_avg']:.2f}s",
            f"${s['cost_usd_total']:.4f}",
            str(s["tokens_total"]),
            str(s["refusal_n_refused"]),
            f"{s['refusal_false_refusal_rate']:.3f}",
        ]) + " |")
    lines.append("")

    # Multi-hop 비교 — PRD §2.2 / §10.7 목표
    mh_summary = {a: s for a, s in summary.items() if "multi_hop_n" in s}
    if mh_summary:
        lines.append("## Multi-hop subset (PRD §2.2 목표: 75%+, hybrid vs vector +30%p)")
        lines.append("| adapter | n | em | f1 |")
        lines.append("|---|---|---|---|")
        for a, s in mh_summary.items():
            lines.append(f"| {a} | {s['multi_hop_n']} | {s['multi_hop_em']:.3f} | {s['multi_hop_f1']:.3f} |")
        lines.append("")

    # hybrid vs vector 차이 — PRD §10.7 +30%p 자동 검증.
    hvv = manifest.get("hybrid_vs_vector") or {}
    if hvv.get("available"):
        met = "✅" if hvv["target_met"] else "❌"
        lines.append("### Hybrid vs Vector (PRD §10.7 목표: +30%p)")
        lines.append(
            f"- EM: hybrid={hvv['hybrid_em']:.3f} vs vector={hvv['vector_em']:.3f} → "
            f"**{hvv['em_diff_pp']:+.1f}%p**"
        )
        lines.append(
            f"- F1: hybrid={hvv['hybrid_f1']:.3f} vs vector={hvv['vector_f1']:.3f} → "
            f"**{hvv['f1_diff_pp']:+.1f}%p**"
        )
        lines.append(f"- 목표 +{hvv['target_diff_pp']:.0f}%p {met}")
        lines.append("")

    # Bridge 데이터 품질 — PRD §10.6 (어댑터 무관, DB 한 번 스냅샷).
    bq = manifest.get("bridge_quality") or {}
    if bq:
        try:
            from eval.metrics.bridge_quality import format_summary_md as _bq_md
            lines.append(_bq_md(bq))
            lines.append("")
        except Exception:  # noqa: BLE001
            pass

    # Main-Hop Efficiency — PRD §10.13.
    mhe = manifest.get("main_hop_efficiency") or {}
    if mhe:
        try:
            from eval.metrics.main_hop_efficiency import format_summary_md as _mhe_md
            lines.append(_mhe_md(mhe))
            lines.append("")
        except Exception:  # noqa: BLE001
            pass

    # Confidence-Weighted Accuracy — PRD §8.3.
    cwa = manifest.get("confidence_weighted") or {}
    if cwa:
        try:
            from eval.metrics.confidence_weighted import format_summary_md as _cwa_md
            lines.append(_cwa_md(cwa))
            lines.append("")
        except Exception:  # noqa: BLE001
            pass

    # Latency — PRD §10.14.
    lat = manifest.get("latency") or {}
    if lat:
        try:
            from eval.metrics.latency import format_summary_md as _lat_md
            lines.append(_lat_md(lat))
            lines.append("")
        except Exception:  # noqa: BLE001
            pass

    lines.append("## 비용 가드 (budget)")
    for adapter, budget in manifest.get("budgets", {}).items():
        lines.append(f"- **{adapter}** {budget}")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def write_per_question_csv(per_q: list[dict], out_path: Path) -> None:
    import csv
    if not per_q:
        out_path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in per_q for k in r.keys()})
    with out_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in per_q:
            w.writerow(r)


# ─── main ───────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--adapters", default="vector,graph,hybrid,sql_vec")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None)

    parser.add_argument("--max-llm-calls", type=int, default=None)
    parser.add_argument("--max-llm-tokens", type=int, default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None,
                        help="어댑터별 USD 한도. 누적 도달 시 다음 row 중단.")
    parser.add_argument("--enable-judge", action="store_true")

    parser.add_argument("--run-id", default=None)
    parser.add_argument("--force", action="store_true",
                        help="resume 무시 — 기존 predictions 삭제 후 재실행")
    args = parser.parse_args()

    if not args.gold.exists():
        print(f"[ERROR] gold 파일 없음: {args.gold}", file=sys.stderr)
        return 2

    gold_rows = load_jsonl(args.gold)
    if args.limit:
        gold_rows = gold_rows[: args.limit]
    if not gold_rows:
        print("[ERROR] gold 비어있음", file=sys.stderr)
        return 2
    print(f"[runner] gold rows: {len(gold_rows)}")

    run_id = args.run_id or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    out_dir = ROOT / "eval" / "reports" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[runner] out dir: {out_dir}")

    adapter_names = [a.strip() for a in args.adapters.split(",") if a.strip()]
    from eval.adapters import get_adapter

    all_per_q: list[dict] = []
    budgets: dict[str, dict] = {}

    for ad_name in adapter_names:
        try:
            adapter = get_adapter(ad_name)
        except ValueError as e:
            print(f"  [{ad_name}] {e}", file=sys.stderr)
            continue

        pred_path = out_dir / f"{ad_name}_predictions.jsonl"
        if args.force and pred_path.exists():
            pred_path.unlink()

        budget = CostBudget(
            max_calls=args.max_llm_calls,
            max_tokens=args.max_llm_tokens,
            max_cost=args.max_cost_usd,
        )
        pred_rows = run_adapter_on_gold(adapter, gold_rows, pred_path, budget=budget)
        budgets[ad_name] = budget.as_dict()

        per_q = compute_per_question_metrics(
            gold_rows, pred_rows,
            top_k=args.top_k, enable_judge=args.enable_judge,
        )
        all_per_q.extend(per_q)

    # 요약·CSV·manifest
    summary = summarize_by_adapter(all_per_q)
    write_per_question_csv(all_per_q, out_dir / "per_question.csv")

    # PRD §10.7 hybrid vs vector +30%p 자동 측정.
    hvv = compute_hybrid_vs_vector(summary)

    # PRD §10.6 Bridge 데이터 품질 스냅샷 (DB 미가용 시 빈 dict).
    try:
        from eval.metrics.bridge_quality import collect_bridge_quality
        bq = collect_bridge_quality()
    except Exception as exc:   # noqa: BLE001
        print(f"  [bridge_quality] skip: {exc}", file=sys.stderr)
        bq = {}

    # 모든 adapter 의 prediction row 합치기 — 추가 메트릭 입력.
    all_pred_rows: list[dict] = []
    for ad_name in adapter_names:
        pred_path = out_dir / f"{ad_name}_predictions.jsonl"
        all_pred_rows.extend(load_jsonl(pred_path))

    # PRD §10.13 Main-Hop Efficiency.
    try:
        from eval.metrics.main_hop_efficiency import main_hop_efficiency
        mhe = main_hop_efficiency(all_pred_rows, all_per_q)
    except Exception as exc:   # noqa: BLE001
        print(f"  [main_hop_efficiency] skip: {exc}", file=sys.stderr)
        mhe = {}

    # PRD §8.3 Confidence-Weighted Accuracy.
    try:
        from eval.metrics.confidence_weighted import confidence_weighted_accuracy
        cwa = confidence_weighted_accuracy(all_per_q, all_pred_rows)
    except Exception as exc:   # noqa: BLE001
        print(f"  [confidence_weighted] skip: {exc}", file=sys.stderr)
        cwa = {}

    # PRD §10.14 Latency (도메인 내 <8s / Cross <12s).
    try:
        from eval.metrics.latency import latency_summary
        lat = latency_summary(all_per_q, gold_rows)
    except Exception as exc:   # noqa: BLE001
        print(f"  [latency] skip: {exc}", file=sys.stderr)
        lat = {}

    manifest = {
        "run_id": run_id,
        "gold": str(args.gold),
        "n_gold": len(gold_rows),
        "adapters": adapter_names,
        "top_k": args.top_k,
        "enable_judge": args.enable_judge,
        "started_at": datetime.now().isoformat(),
        "git": git_info(),
        "budgets": budgets,
        "hybrid_vs_vector": hvv,
        "bridge_quality": bq,
        "main_hop_efficiency": mhe,
        "confidence_weighted": cwa,
        "latency": lat,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    write_summary_md(summary, manifest, out_dir / "summary.md")

    print(f"\n[runner] 완료. summary: {out_dir / 'summary.md'}")
    for a, s in summary.items():
        print(f"  {a}: em={s['em']:.3f} f1={s['f1']:.3f} hits={s['hits@k']:.3f} "
              f"faith={s['faithfulness']:.3f} cost=${s['cost_usd_total']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
