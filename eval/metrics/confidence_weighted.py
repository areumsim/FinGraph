"""Confidence-Weighted Accuracy 메트릭 (PRD §8.3).

답변 근거 엣지의 ``confidence_score`` 가중 EM/F1 평균.

엣지 confidence 는 adapter 가 ``evidence[].score`` 또는 ``answer_confidence`` 로 노출.
score 가 0/None 이면 1.0 으로 대체 (회귀 비교 가능).

PRD §6.7: candidate 엣지 인용 → confidence < 1.0 → 가중치 감소. 즉
"validated 만 인용한 정답" 이 "candidate 만 인용한 정답" 보다 후하게 평가됨.
"""

from __future__ import annotations

from statistics import fmean
from typing import Any, Iterable


def _row_confidence(pred_row: dict) -> float:
    """row 의 confidence 대표값 — evidence.score 평균 (없으면 answer_confidence)."""
    ev = pred_row.get("evidence") or []
    if ev:
        scores = []
        for e in ev:
            s = e.get("score") if isinstance(e, dict) else None
            try:
                if s is not None:
                    scores.append(float(s))
            except (TypeError, ValueError):
                continue
        if scores:
            return fmean(scores)
    ac = pred_row.get("answer_confidence")
    if ac is None:
        return 1.0
    try:
        return float(ac)
    except (TypeError, ValueError):
        return 1.0


def confidence_weighted_accuracy(
    per_q: Iterable[dict],
    pred_rows: Iterable[dict],
) -> dict[str, Any]:
    """adapter 별 conf-weighted EM/F1.

    weighted_em = mean(em_i * conf_i)
    weighted_f1 = mean(f1_i * conf_i)

    Args:
        per_q: ``{adapter, qid, em, f1}`` 메트릭 행.
        pred_rows: ``{adapter, qid, evidence, answer_confidence}`` 예측 행 —
            conf 추출용.

    Returns:
        ``{adapter: {n, em, f1, conf_em, conf_f1, conf_avg}}``
    """
    conf_map: dict[tuple[str, str], float] = {}
    for p in pred_rows:
        a = p.get("adapter", "")
        qid = p.get("qid", "")
        if a and qid:
            conf_map[(a, qid)] = _row_confidence(p)

    out: dict[str, dict[str, Any]] = {}
    for m in per_q:
        a = m.get("adapter", "")
        if not a:
            continue
        slot = out.setdefault(a, {
            "em": [], "f1": [], "conf_em": [], "conf_f1": [], "conf": [],
        })
        em = float(m.get("em") or 0.0)
        f1 = float(m.get("f1") or 0.0)
        c  = conf_map.get((a, m.get("qid", "")), 1.0)
        slot["em"].append(em)
        slot["f1"].append(f1)
        slot["conf"].append(c)
        slot["conf_em"].append(em * c)
        slot["conf_f1"].append(f1 * c)

    final: dict[str, Any] = {}
    for a, slot in out.items():
        n = len(slot["em"])
        final[a] = {
            "n":       n,
            "em":      round(fmean(slot["em"]), 4),
            "f1":      round(fmean(slot["f1"]), 4),
            "conf_em": round(fmean(slot["conf_em"]), 4),
            "conf_f1": round(fmean(slot["conf_f1"]), 4),
            "conf_avg": round(fmean(slot["conf"]), 4),
        }
    return final


def format_summary_md(metric: dict[str, Any]) -> str:
    lines = ["## Confidence-Weighted Accuracy (PRD §8.3)"]
    if not metric:
        lines.append("- (어댑터 행 없음)")
        return "\n".join(lines)
    lines.append("| adapter | n | em | conf_em | f1 | conf_f1 | conf_avg |")
    lines.append("|---|---|---|---|---|---|---|")
    for a, s in metric.items():
        lines.append(
            f"| {a} | {s['n']} | {s['em']:.3f} | {s['conf_em']:.3f} | "
            f"{s['f1']:.3f} | {s['conf_f1']:.3f} | {s['conf_avg']:.3f} |"
        )
    return "\n".join(lines)


__all__ = ["confidence_weighted_accuracy", "format_summary_md"]
