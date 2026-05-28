"""LLM 없는 결정적 답변 brief — Synthesizer LLM 의존도 ↓.

설계 메모 (이전 v2/agent/answering.py 의 핵심 아이디어 흡수):
- 도구 결과 (financials/graph 출력) 만으로 자연어 brief 한 줄 생성
- LLM 비용 한도 초과 / synthesis 실패 시 사용자에게 즉시 fallback 답변 제공
- relation 표기는 'X → Y (한국어 관계명)' 형태

코오롱 도메인 시나리오/문구 제외 — 금융 일반 도메인 패턴만.

사용 패턴 (synthesizer 의 LLM 실패 시):
    brief = build_deterministic_brief(state)
    state["answer"] = brief  # LLM 못 부를 때 폴백
"""

from __future__ import annotations

from typing import Any


# ─── 관계명 한국어 ─────────────────────────────────────────
# ontology/relations.yaml 의 관계 타입 → 사람 읽기 좋은 한국어
_REL_KOR: dict[str, str] = {
    "SUBSIDIARY_OF":         "자회사",
    "RELATED_TO":            "관계회사",
    "MAJOR_SHAREHOLDER_OF":  "최대주주",
    "EXECUTIVE_OF":          "임원직",
    "HAS_CEO":               "대표이사",
    "LISTED_IN":             "상장시장",
    "IN_INDUSTRY":           "산업",
    "BELONGS_TO_GROUP":      "기업집단",
    "PARTNER_OF":            "협력",
    "COMPETES_WITH":         "경쟁",
    "INVESTED_IN":           "투자",
    "PRODUCES":              "생산",
    "MENTIONS":              "언급",
    "CO_MENTIONED_WITH":     "공동언급",
    "HAS_ESG_RATING":        "ESG 등급",
    "HOLDS_PATENT":          "특허보유",
}


def rel_type_kor(rtype: str) -> str:
    return _REL_KOR.get(rtype or "", "")


def _clamp01(value: Any) -> float:
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, v)), 4)


def avg_relation_confidence(items: list[dict]) -> float:
    """관계 list 의 confidence 평균 (0~1)."""
    if not items:
        return 0.0
    total = sum(float(r.get("confidence") or 0.0) for r in items)
    return _clamp01(total / len(items))


def format_relation(rel: dict) -> str:
    """relation dict → 한국어 한 줄.

    예: '삼성전자 → SK하이닉스 (협력) [conf=0.85]'
    """
    head = str(rel.get("head") or rel.get("head_corp_code") or "").strip()
    rtype = str(rel.get("relation") or rel.get("rel_type") or "").strip()
    tail = str(rel.get("tail") or rel.get("tail_corp_code") or "").strip()
    if not head or not tail:
        return ""
    kor = rel_type_kor(rtype)
    base = f"{head} → {tail} ({kor})" if kor else f"{head} → {tail} ({rtype})"
    conf = rel.get("confidence")
    if conf is not None:
        return f"{base} [conf={_clamp01(conf):.2f}]"
    return base


def format_tool_result(tool: str, result: Any) -> list[str]:
    """단일 도구 결과 → 1~5 줄 brief.

    도구별 결과 schema 가 달라 케이스 분기. 너무 길면 truncate.
    """
    out: list[str] = []
    if not result:
        return out
    label_map = {
        "list_subsidiaries":      "자회사",
        "list_parents":           "모회사",
        "get_executives":         "임원",
        "get_companies_of_person": "임원직",
        "get_major_shareholders": "최대주주",
        "list_mentioning_news":   "언급 뉴스",
        "list_cooccurring":       "공동언급 회사",
        "list_group_members":     "기업집단 계열사",
    }
    label = label_map.get(tool, tool)

    if isinstance(result, dict):
        out.append(f"- {label}: {_summarize_dict(result, 80)}")
        return out

    if isinstance(result, list):
        n = len(result)
        out.append(f"- {label} ({n}건)")
        for r in result[:5]:
            if not isinstance(r, dict):
                continue
            line = _summarize_dict(r, 80)
            if line:
                out.append(f"  · {line}")
        if n > 5:
            out.append(f"  · … 외 {n - 5}건")
    return out


def _summarize_dict(d: dict, maxlen: int = 80) -> str:
    """dict 핵심 키 1~3개를 'k=v, k=v' 로 한 줄. 우선순위 key 가 있으면 먼저."""
    if not d:
        return ""
    priority = ("name", "corp_code", "company_name", "child_name", "title", "role",
                "ownership_pct", "value", "snapshot_year", "published_at")
    parts: list[str] = []
    for k in priority:
        if k in d and d[k] not in (None, ""):
            parts.append(f"{k}={d[k]}")
            if len(parts) >= 3:
                break
    if not parts:
        # fallback — 첫 2 키
        for k in list(d)[:2]:
            v = d.get(k)
            if v not in (None, ""):
                parts.append(f"{k}={v}")
    s = ", ".join(parts)
    return s[:maxlen] + ("…" if len(s) > maxlen else "")


def build_deterministic_brief(state: dict) -> str:
    """tool_results + evidence_chunks → LLM 없는 자연어 brief.

    Synthesizer 의 LLM 호출 실패 / 비용 초과 / Budget Exceeded 시 fallback.
    state 는 AgentState dict.
    """
    q = state.get("question") or ""
    kind = state.get("question_kind") or "unknown"
    targets = state.get("target_companies") or []

    lines: list[str] = []
    lines.append(f"질문 유형: {kind}")
    if targets:
        lines.append(f"식별된 회사: {', '.join(targets[:5])}")

    tool_results = state.get("tool_results") or []
    if tool_results:
        lines.append("")
        lines.append("[도구 결과]")
        for t in tool_results:
            lines.extend(format_tool_result(t.get("tool", ""), t.get("result")))

    evidence = state.get("evidence_chunks") or []
    if evidence:
        lines.append("")
        lines.append(f"[관련 본문 {len(evidence)}건 발견]")
        for c in evidence[:3]:
            corp = c.get("corp_code") or ""
            year = c.get("fiscal_year") or ""
            section = (c.get("section") or "")[:25]
            score = c.get("score") or 0.0
            preview = (c.get("text") or "")[:140].replace("\n", " ")
            lines.append(f"  · [{corp}/{year}/{section}] (sim={score:.2f}) {preview}…")

    if not tool_results and not evidence:
        lines.append("")
        lines.append("도구 결과·근거 본문이 없습니다. 질문을 더 구체화해주세요.")

    lines.append("")
    lines.append("※ LLM 답변 합성이 비활성/실패 상태입니다. 위 도구 출력을 직접 확인하세요.")
    return "\n".join(lines)


__all__ = [
    "rel_type_kor", "avg_relation_confidence", "format_relation",
    "format_tool_result", "build_deterministic_brief",
]
