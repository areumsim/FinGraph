"""AutoNexusGraph Streamlit Web UI — 멀티턴 채팅 + 출처 + 비용 노출.

설계 메모 (이전 web/app.py 패턴 단순화):
- 채팅형 multi-turn (st.chat_message, st.chat_input)
- session_state thread_id (PG chat.conversations 와 1:1)
- 답변에 citation expander + grounding 경고
- 사이드바: 최근 대화 목록 + LLM 비용 누적 + provider 정보

실행:
    pip install streamlit
    streamlit run src/autonexusgraph/ui/app.py --server.port 8501 \\
        --server.address 0.0.0.0
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# src/ 를 path 에 추가 (streamlit 진입 시)
_ROOT = Path(__file__).resolve().parents[3]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st

from autonexusgraph.ui.storage import (
    get_or_create_thread_id, reset_thread,
    load_history, persist_turn, list_recent_threads,
    set_conversation_title, generate_title_from_question,
)
from autonexusgraph.ui.components import (
    render_citations, render_grounding_warning, render_agent_trace,
    render_cost_badge, render_provider_info, render_sample_questions,
    render_feedback_buttons, render_progress_chip, node_label,
    render_clarification, render_cost_approval,
)


st.set_page_config(page_title="AutoNexusGraph", layout="wide")


# ─── session_state init ───────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []
if "cumulative_cost_usd" not in st.session_state:
    st.session_state.cumulative_cost_usd = 0.0
if "last_turn_cost_usd" not in st.session_state:
    st.session_state.last_turn_cost_usd = 0.0

thread_id = get_or_create_thread_id()


# ─── 사이드바 ─────────────────────────────────────────────────
with st.sidebar:
    st.title("AutoNexusGraph")
    st.caption(f"thread: `{thread_id[:20]}…`")

    if st.button("새 대화 시작"):
        reset_thread()
        st.rerun()

    st.divider()
    # 도메인 모드 — finance / auto / cross_domain (PRD v2.0 AutoGraph).
    if "domain_mode" not in st.session_state:
        st.session_state.domain_mode = "auto-detect"
    st.session_state.domain_mode = st.radio(
        "도메인 모드",
        options=["auto-detect", "finance", "auto", "cross_domain"],
        index=["auto-detect", "finance", "auto", "cross_domain"].index(
            st.session_state.domain_mode
        ),
        help="auto-detect 면 질문 키워드로 router 가 자동 판정. 자동차 도메인은 'auto'.",
    )

    st.divider()
    render_cost_badge(
        st.session_state.cumulative_cost_usd,
        st.session_state.last_turn_cost_usd,
    )
    render_provider_info()

    st.divider()
    st.subheader("최근 대화")
    for t in list_recent_threads(limit=8):
        label = f"{t['title'] or t['thread_id'][:14]} ({t['n_messages']})"
        if st.button(label, key=f"thread_{t['thread_id']}"):
            st.session_state.thread_id = t["thread_id"]
            st.session_state.messages = load_history(t["thread_id"])
            st.rerun()

    sample = render_sample_questions()


# ─── 메인 — 대화 영역 ────────────────────────────────────────
st.title("금융 GraphRAG 에이전트")
st.caption("한국 상장사 공시·재무 데이터 기반 멀티홉 추론. PRD §2.1 의 예시 질문 참조.")

# 이전 메시지 렌더
for idx, m in enumerate(st.session_state.messages):
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("citations"):
            render_citations(m["citations"])
        if m.get("agent_trace"):
            render_agent_trace(m["agent_trace"])
        if m.get("grounding"):
            render_grounding_warning(m["grounding"])
        if m["role"] == "assistant" and m.get("id"):
            render_feedback_buttons(m["id"], key_prefix=f"hist_{idx}")

# 새 입력
user_input = st.chat_input("질문을 입력하세요…")
if sample and not user_input:
    user_input = sample

if user_input:
    # 첫 turn 이면 title 생성 (LLM 1콜) → conversation title 갱신
    is_first_turn = len(st.session_state.messages) == 0

    # user message
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)
    user_msg_id = persist_turn(thread_id, "user", user_input)

    if is_first_turn:
        try:
            title = generate_title_from_question(user_input)
            if title:
                set_conversation_title(thread_id, title)
        except Exception:
            pass

    # agent run — 노드별 진행 표시 (PRD §7.6.5) + HITL interrupt (PRD §7.5.6)
    with st.chat_message("assistant"):
        try:
            from autonexusgraph.agents import run_agent_stream, run_agent_resume_stream
            with st.status("분석 중…", expanded=True) as status:
                last_state = None
                interrupted_payload = None
                _dm = st.session_state.get("domain_mode") or "auto-detect"
                _domain = None if _dm == "auto-detect" else _dm
                for node, state in run_agent_stream(
                    user_input, thread_id=thread_id,
                    history=st.session_state.messages[-10:],
                    domain=_domain,
                ):
                    last_state = state
                    if node == "__final__":
                        status.update(label="✅ 완료", state="complete")
                        break
                    if node == "__error__":
                        status.update(label="❌ 오류", state="error")
                        break
                    if node == "__interrupt__":
                        interrupted_payload = state.get("pending_interrupt")
                        status.update(label="⏸️ 사용자 응답 대기", state="running")
                        break
                    partial = {
                        "question_kind": state.get("question_kind"),
                        "target_companies": state.get("target_companies"),
                        "n_tool_results": len(state.get("tool_results") or []),
                        "cost_usd": float(state.get("llm_usage_usd") or 0.0),
                    }
                    st.write(render_progress_chip(node, partial))
                    status.update(label=node_label(node))

            # HITL — interrupt 종류별 응답 dialog → resume
            if interrupted_payload:
                kind = interrupted_payload.get("kind")
                key_prefix = f"{thread_id}_{len(st.session_state.messages)}"
                resume_value: any = None
                if kind == "company_clarification":
                    idx = render_clarification(interrupted_payload, key_prefix=key_prefix)
                    if idx is None:
                        st.stop()
                    resume_value = {"index": idx}
                elif kind == "cost_approval":
                    approved = render_cost_approval(interrupted_payload, key_prefix=key_prefix)
                    if approved is None:
                        st.stop()
                    resume_value = approved
                else:
                    st.error(f"알 수 없는 interrupt 종류: {kind}")
                    st.stop()
                # resume
                with st.status("응답 처리 중…", expanded=True) as status:
                    for node, state in run_agent_resume_stream(thread_id, resume_value):
                        last_state = state
                        if node == "__final__":
                            status.update(label="✅ 완료", state="complete")
                            break
                        if node == "__error__":
                            status.update(label="❌ resume 실패", state="error")
                            break
                        partial = {
                            "question_kind": state.get("question_kind"),
                            "target_companies": state.get("target_companies"),
                            "n_tool_results": len(state.get("tool_results") or []),
                            "cost_usd": float(state.get("llm_usage_usd") or 0.0),
                        }
                        st.write(render_progress_chip(node, partial))
                        status.update(label=node_label(node))

            state = last_state or {}
            answer = state.get("answer") or "(빈 응답)"
            citations = state.get("citations") or []
            turn_cost = float(state.get("llm_usage_usd") or 0.0)
            grounding = state.get("grounding") or {}
            trace = {
                "question_kind": state.get("question_kind"),
                "target_companies": state.get("target_companies"),
                "n_tool_results": len(state.get("tool_results") or []),
                "cost_usd": turn_cost,
                "aborted_reason": state.get("aborted_reason"),
                "n_replans": state.get("n_replans"),
                "validation_status": state.get("validation_status"),
                "validation_issues": state.get("validation_issues"),
            }
        except Exception as e:
            answer = f"❌ 에이전트 실행 실패: {type(e).__name__}: {e}"
            citations = []
            turn_cost = 0.0
            grounding = {}
            trace = {"aborted_reason": "exception"}

        st.markdown(answer)
        render_citations(citations)
        render_agent_trace(trace)
        render_grounding_warning(grounding)

    # 적재 + state 갱신
    asst_msg_id = persist_turn(thread_id, "assistant", answer,
                                citations=citations, agent_trace=trace)
    if asst_msg_id:
        render_feedback_buttons(asst_msg_id, key_prefix="new")
    st.session_state.messages.append({
        "id": asst_msg_id,
        "role": "assistant", "content": answer,
        "citations": citations, "agent_trace": trace,
        "grounding": grounding,
    })
    st.session_state.cumulative_cost_usd += turn_cost
    st.session_state.last_turn_cost_usd = turn_cost
