"""Latency 메트릭 (PRD §10.14).

도메인 내 < 8s, Cross-Domain < 12s 임계 통과율 + 분포 통계.

runner 가 매 row 의 ``latency_sec`` 을 기록하므로 본 메트릭은 그것을 그룹별로
요약한다 (adapter × domain).
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable


# PRD §10.14 임계.
THRESHOLD_DOMAIN_INTERNAL_SEC = 8.0
THRESHOLD_CROSS_DOMAIN_SEC    = 12.0


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return float(s[k])


def latency_summary(per_q: Iterable[dict],
                    gold_rows: Iterable[dict] | None = None) -> dict[str, Any]:
    """adapter × domain 별 latency 분포 + 임계 통과율.

    Args:
        per_q: ``{adapter, qid, latency_sec}`` per-question 메트릭.
        gold_rows: 옵션 — ``{qid, domain}`` 매핑. 없으면 domain='unknown'.

    Returns:
        ``{adapter: {n, avg, p50, p95, max, target_internal_pass_rate,
                     target_cross_pass_rate, by_domain: {...}}}``
    """
    domain_map: dict[str, str] = {}
    if gold_rows is not None:
        for g in gold_rows:
            domain_map[g.get("qid", "")] = g.get("domain", "unknown") or "unknown"

    by_adapter: dict[str, list[tuple[str, float]]] = {}
    for r in per_q:
        a = r.get("adapter", "")
        if not a:
            continue
        qid = r.get("qid", "")
        dom = domain_map.get(qid, "unknown")
        try:
            sec = float(r.get("latency_sec") or 0.0)
        except (TypeError, ValueError):
            sec = 0.0
        by_adapter.setdefault(a, []).append((dom, sec))

    out: dict[str, Any] = {
        "threshold_internal_sec": THRESHOLD_DOMAIN_INTERNAL_SEC,
        "threshold_cross_sec":    THRESHOLD_CROSS_DOMAIN_SEC,
    }
    for a, items in by_adapter.items():
        secs = [s for _, s in items]
        internal = [s for d, s in items if d in ("finance", "auto", "unknown")]
        cross    = [s for d, s in items if d == "cross_domain"]

        internal_pass = sum(1 for s in internal if s < THRESHOLD_DOMAIN_INTERNAL_SEC)
        cross_pass    = sum(1 for s in cross    if s < THRESHOLD_CROSS_DOMAIN_SEC)

        rec = {
            "n":        len(secs),
            "avg":      round(statistics.fmean(secs), 3) if secs else 0.0,
            "p50":      round(_percentile(secs, 0.5), 3),
            "p95":      round(_percentile(secs, 0.95), 3),
            "max":      round(max(secs), 3) if secs else 0.0,
            "target_internal_pass_rate": (
                round(internal_pass / len(internal), 4) if internal else None
            ),
            "target_cross_pass_rate": (
                round(cross_pass / len(cross), 4) if cross else None
            ),
        }
        # domain 별 평균.
        by_domain: dict[str, dict[str, Any]] = {}
        for d in {x[0] for x in items}:
            xs = [s for dd, s in items if dd == d]
            by_domain[d] = {
                "n":   len(xs),
                "avg": round(statistics.fmean(xs), 3) if xs else 0.0,
                "p95": round(_percentile(xs, 0.95), 3),
            }
        rec["by_domain"] = by_domain
        out[a] = rec
    return out


def format_summary_md(metric: dict[str, Any]) -> str:
    th_i = metric.get("threshold_internal_sec", THRESHOLD_DOMAIN_INTERNAL_SEC)
    th_c = metric.get("threshold_cross_sec",    THRESHOLD_CROSS_DOMAIN_SEC)
    lines = [f"## Latency (PRD §10.14: 도메인 내 <{th_i}s / Cross <{th_c}s)"]
    lines.append("| adapter | n | avg | p50 | p95 | max | internal pass | cross pass |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for a, s in metric.items():
        if not isinstance(s, dict) or "avg" not in s:
            continue
        ip = s.get("target_internal_pass_rate")
        cp = s.get("target_cross_pass_rate")
        ip_s = f"{ip:.1%}" if ip is not None else "n/a"
        cp_s = f"{cp:.1%}" if cp is not None else "n/a"
        lines.append(
            f"| {a} | {s['n']} | {s['avg']}s | {s['p50']}s | {s['p95']}s | "
            f"{s['max']}s | {ip_s} | {cp_s} |"
        )
    return "\n".join(lines)


__all__ = [
    "latency_summary", "format_summary_md",
    "THRESHOLD_DOMAIN_INTERNAL_SEC", "THRESHOLD_CROSS_DOMAIN_SEC",
]
