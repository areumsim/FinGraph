"""Streamlit 렌더링 component — citation expander / cost badge / grounding warning.

설계 메모 (이전 web/ui.py 의 카드 패턴 — namespace selector 같은 BNT 특화는 제외):
- 답변 본문에 citation expander
- 사이드바에 비용 누적 + LLM provider 표시
- grounding warning 이 있으면 노란 박스
"""

from __future__ import annotations

from typing import Any


def render_citations(citations: list[dict]) -> None:
    """답변 아래 출처 expander."""
    import streamlit as st
    if not citations:
        return
    with st.expander(f"출처 {len(citations)}건"):
        for i, c in enumerate(citations, 1):
            corp = c.get("corp_code") or ""
            year = c.get("fiscal_year") or ""
            section = (c.get("section") or "")[:40]
            score = c.get("score")
            rcept = c.get("rcept_no") or ""
            score_s = f" sim={score:.3f}" if score is not None else ""
            st.markdown(
                f"**[{i}]** `corp={corp}` `year={year}` "
                f"`section={section}` `rcept={rcept}`{score_s}"
            )


def render_grounding_warning(grounding: dict | None) -> None:
    """grounding.ok=False 시 노란 박스 + 사유 노출."""
    import streamlit as st
    if not grounding:
        return
    if grounding.get("ok"):
        return
    warnings = grounding.get("warnings") or []
    if not warnings:
        return
    st.warning(
        "⚠️ 답변 근거 검증 경고: "
        + ", ".join(warnings)
        + f" (overlap={grounding.get('overlap_ratio', 0):.2f}, "
        f"cit={grounding.get('citation_count', 0)})"
    )


def render_agent_trace(trace: dict[str, Any]) -> None:
    """agent_trace 요약 — question_kind / targets / tool 호출 수 / 비용."""
    import streamlit as st
    if not trace:
        return
    items: list[str] = []
    if trace.get("question_kind"):
        items.append(f"유형: `{trace['question_kind']}`")
    targets = trace.get("target_companies") or trace.get("targets") or []
    if targets:
        items.append(f"회사: `{', '.join(targets[:3])}`")
    if trace.get("n_tool_results") is not None:
        items.append(f"도구: `{trace['n_tool_results']}`")
    if trace.get("cost_usd") is not None:
        items.append(f"비용: `${trace['cost_usd']:.4f}`")
    if trace.get("aborted_reason"):
        items.append(f"⚠️ aborted: `{trace['aborted_reason']}`")
    if items:
        st.caption(" · ".join(items))


def render_cost_badge(cumulative_usd: float, turn_usd: float = 0.0) -> None:
    """사이드바 — 세션 비용 누적 + 최근 turn 비용."""
    import streamlit as st
    st.metric(
        label="누적 LLM 비용 (USD)",
        value=f"${cumulative_usd:.4f}",
        delta=f"+${turn_usd:.4f}" if turn_usd else None,
    )


def render_provider_info() -> None:
    """사이드바 — 현재 LLM provider / model 표시."""
    import streamlit as st
    from ..config import get_settings
    s = get_settings()
    st.caption(f"LLM: `{s.llm_provider}` / `{s.llm_model}`")
    st.caption(f"임베딩: `{s.embedding_url}` (dim {s.embedding_dim})")


# ── 노드 진행 표시 (PRD §7.6.5) ─────────────────────────────
_NODE_LABEL = {
    "triage":       "🔍 Triage",
    "planner":      "🧭 Planner",
    "executor":     "🛠️ Executor",
    "synthesizer":  "✍️ Synthesizer",
    "validator":    "✅ Validator",
    "replan":       "♻️ Replan",
    "finalize":     "🏁 Finalize",
    "__final__":    "🏁 완료",
    "__error__":    "❌ 오류",
}


def node_label(node: str) -> str:
    return _NODE_LABEL.get(node, f"⚙️ {node}")


def render_progress_chip(node: str, partial: dict | None = None) -> str:
    """st.status 내부에 보여줄 한 줄 — 노드 + 핵심 partial state."""
    label = node_label(node)
    if not partial:
        return label
    bits: list[str] = [label]
    if partial.get("question_kind"):
        bits.append(f"kind=`{partial['question_kind']}`")
    if partial.get("target_companies"):
        bits.append(f"회사={len(partial['target_companies'])}")
    if partial.get("n_tool_results"):
        bits.append(f"도구={partial['n_tool_results']}")
    cost = partial.get("cost_usd")
    if cost is not None and cost > 0:
        bits.append(f"비용=${cost:.4f}")
    return " · ".join(bits)


def render_cost_approval(payload: dict, *, key_prefix: str) -> bool | None:
    """비용 승인 dialog — PRD §7.5.6 cost_approval.

    payload: agents.interrupts.make_cost_approval_payload 산출
    반환: True (승인) / False (거절) / None (아직 선택 안 함)
    """
    import streamlit as st
    state_key = f"cost_{key_prefix}"
    if st.session_state.get(state_key) is not None:
        return st.session_state[state_key]

    cost = float(payload.get("estimated_cost_usd") or 0.0)
    st.warning(
        f"💰 {payload.get('prompt') or '비용 승인 필요'}\n\n"
        f"예상 비용: **${cost:.4f}**\n\n"
        f"plan: {payload.get('plan_summary') or '-'}"
    )
    cols = st.columns([1, 1, 5])
    with cols[0]:
        if st.button("✅ 승인", key=f"approve_{key_prefix}"):
            st.session_state[state_key] = True
            return True
    with cols[1]:
        if st.button("❌ 거절", key=f"reject_{key_prefix}"):
            st.session_state[state_key] = False
            return False
    return None


def render_clarification(payload: dict, *, key_prefix: str) -> int | None:
    """모호한 회사명 후보 라디오 — PRD §7.5.6 / §7.6.5.

    payload: agents.interrupts.make_clarification_payload 산출
    반환: 사용자가 선택한 candidate index (또는 None — 아직 선택 안 함)
    """
    import streamlit as st
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    state_key = f"clarify_{key_prefix}"
    if st.session_state.get(state_key) is not None:
        return st.session_state[state_key]

    st.warning(f"🤔 {payload.get('prompt') or '회사를 선택해주세요'}")
    labels = [
        f"{c.get('name') or c.get('corp_name','')}  "
        f"(corp={c.get('corp_code')}, "
        f"종목={c.get('stock_code') or '-'}, "
        f"시장={c.get('market') or '-'})"
        for c in candidates
    ]
    choice = st.radio("후보", labels, key=f"radio_{key_prefix}", index=None)
    if choice is None:
        return None
    if st.button("선택 확정", key=f"submit_{key_prefix}"):
        idx = labels.index(choice)
        st.session_state[state_key] = idx
        return idx
    return None


def render_feedback_buttons(message_id: int | None, *, key_prefix: str) -> None:
    """답변 아래 👍/👎/📝 — PRD §7.6.5. message_id 없으면 비활성.

    record_feedback 호출은 storage.record_feedback 로 위임 (DB 실패 fail-soft).
    """
    import streamlit as st
    from .storage import record_feedback

    if not message_id:
        return
    cols = st.columns([1, 1, 6])
    state_key = f"fb_{key_prefix}_{message_id}"
    sent = st.session_state.get(state_key)

    with cols[0]:
        if st.button("👍", key=f"up_{key_prefix}_{message_id}",
                     disabled=(sent == "up")):
            if record_feedback(message_id, +1, None):
                st.session_state[state_key] = "up"
                st.toast("피드백 기록됨", icon="👍")
    with cols[1]:
        if st.button("👎", key=f"down_{key_prefix}_{message_id}",
                     disabled=(sent == "down")):
            if record_feedback(message_id, -1, None):
                st.session_state[state_key] = "down"
                st.toast("피드백 기록됨", icon="👎")
    with cols[2]:
        with st.popover("📝 의견"):
            txt_key = f"fb_text_{key_prefix}_{message_id}"
            comment = st.text_area("의견을 남겨주세요", key=txt_key, height=80)
            if st.button("저장", key=f"fb_save_{key_prefix}_{message_id}"):
                if comment and record_feedback(message_id, 0, comment.strip()):
                    st.session_state[state_key] = "comment"
                    st.toast("의견 저장됨", icon="📝")


def render_sample_questions() -> str | None:
    """샘플 질문 클릭 시 그 텍스트 반환 (input 으로 전달)."""
    import streamlit as st
    samples = [
        "삼성전자 2024년 매출은?",
        "삼성전자 자회사 중 매출 1조 이상은?",
        "현대자동차의 주요 사업 위험요인은?",
        "이재용이 임원인 회사들은?",
        "삼성그룹 계열사 중 ESG A+ 등급은?",
    ]
    with st.sidebar.expander("샘플 질문"):
        for q in samples:
            if st.button(q, key=f"sample_{q[:20]}"):
                return q
    return None
