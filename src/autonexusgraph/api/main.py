"""FastAPI 진입점 — 에이전트 채팅 API.

엔드포인트:
- POST /chat                : 단일 turn 실행 → 답변 + 인용 + 비용
- GET  /threads/{id}        : 대화 히스토리 조회 (PG chat.messages)
- POST /threads/{id}/message: 멀티턴 — history 자동 주입

응답 메타에 cost_usd / tokens 포함 (사용자 명시 — 모든 호출 비용 가시화).

기동:
    uvicorn autonexusgraph.api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agents import (
    run_agent,
    run_agent_resume_stream,
    run_agent_stream,
)
from ..db.postgres import get_pool


log = logging.getLogger(__name__)


app = FastAPI(title="AutoNexusGraph Agent API", version="0.1")


# ── Request/Response 모델 ───────────────────────────────────
class ChatRequest(BaseModel):
    thread_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    message: str
    use_history: bool = True
    # 도메인 hint — 'finance' | 'auto' | 'cross_domain'. 미지정 시 router 자동 판정.
    domain: str | None = None


class ChatResponse(BaseModel):
    thread_id: str
    answer: str
    citations: list[dict]
    question_kind: str | None = None
    target_companies: list[str] = []
    domain: str | None = None
    cost_usd: float = 0.0
    aborted_reason: str | None = None
    n_tool_results: int = 0


# ── chat endpoint ───────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    """단일 대화 turn — agent 실행 + history 적재."""
    history = _load_history(req.thread_id) if req.use_history else []
    try:
        state = run_agent(req.message, thread_id=req.thread_id,
                          history=history, domain=req.domain)
    except Exception as e:
        log.exception("[chat] agent failed")
        raise HTTPException(500, f"agent failed: {e}")

    # PG chat.messages 에 user + assistant 두 turn 적재
    _persist_turn(req.thread_id, "user", req.message, citations=None, trace=None)
    _persist_turn(req.thread_id, "assistant", state.get("answer", ""),
                   citations=state.get("citations"),
                   trace={"question_kind": state.get("question_kind"),
                          "target_companies": state.get("target_companies"),
                          "domain": state.get("domain"),
                          "n_tool_results": len(state.get("tool_results") or []),
                          "cost_usd": state.get("llm_usage_usd"),
                          "aborted_reason": state.get("aborted_reason")})

    return ChatResponse(
        thread_id=req.thread_id,
        answer=state.get("answer", ""),
        citations=state.get("citations") or [],
        question_kind=state.get("question_kind"),
        target_companies=state.get("target_companies") or [],
        domain=state.get("domain"),
        cost_usd=float(state.get("llm_usage_usd") or 0.0),
        aborted_reason=state.get("aborted_reason"),
        n_tool_results=len(state.get("tool_results") or []),
    )


# ── chat stream (SSE — PRD §7.6.5) ──────────────────────────
@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE — 노드 진입마다 partial state 한 줄. 마지막에 data: [DONE].

    이벤트 형태:
        data: {"node": "triage", "question_kind": "factual", ...}\\n\\n
        ...
        data: {"node": "__final__", "answer": "...", "citations": [...], "cost_usd": ...}\\n\\n
        data: [DONE]\\n\\n
    """
    history = _load_history(req.thread_id) if req.use_history else []

    def _gen():
        try:
            user_msg_logged = False
            for node, st in run_agent_stream(req.message,
                                              thread_id=req.thread_id,
                                              history=history,
                                              domain=req.domain):
                payload: dict[str, Any] = {
                    "node": node,
                    "question_kind": st.get("question_kind"),
                    "target_companies": st.get("target_companies") or [],
                    "n_tool_results": len(st.get("tool_results") or []),
                    "cost_usd": float(st.get("llm_usage_usd") or 0.0),
                    "n_replans": st.get("n_replans"),
                    "validation_status": st.get("validation_status"),
                }
                if node == "__interrupt__":
                    # HITL — UI 가 응답을 받아 /chat/resume 호출하도록 유도
                    payload["pending_interrupt"] = st.get("pending_interrupt") or {}
                    if not user_msg_logged:
                        _persist_turn(req.thread_id, "user", req.message,
                                       citations=None, trace=None)
                        user_msg_logged = True
                if node == "__final__":
                    payload["answer"] = st.get("answer", "")
                    payload["citations"] = st.get("citations") or []
                    payload["grounding"] = st.get("grounding") or {}
                    payload["validation_issues"] = st.get("validation_issues") or []
                    # 최종 적재 — user + assistant 두 turn (user 는 처음 한 번만)
                    if not user_msg_logged:
                        _persist_turn(req.thread_id, "user", req.message,
                                       citations=None, trace=None)
                        user_msg_logged = True
                    _persist_turn(req.thread_id, "assistant", payload["answer"],
                                   citations=payload["citations"],
                                   trace={"question_kind": payload["question_kind"],
                                          "target_companies": payload["target_companies"],
                                          "n_tool_results": payload["n_tool_results"],
                                          "cost_usd": payload["cost_usd"],
                                          "n_replans": payload["n_replans"],
                                          "validation_status": payload["validation_status"]})
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:   # noqa: BLE001
            log.exception("[chat_stream] failed")
            err = {"node": "__error__", "error": f"{type(exc).__name__}: {exc}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── chat resume (HITL — PRD §7.5.6) ─────────────────────────
class ResumeRequest(BaseModel):
    thread_id: str
    response: Any   # corp_code 8자리 str / int(index) / dict({corp_code: ...})


@app.post("/chat/resume")
def chat_resume(req: ResumeRequest) -> StreamingResponse:
    """interrupt 후 사용자가 응답한 값으로 graph 재개. SSE 스트림.

    LangGraph + langgraph.types.Command 필요. 폴백 환경에서는 클라이언트가
    응답을 새 /chat 호출에 합쳐 보내는 패턴 권장 (UI 가 처리).
    """
    def _gen():
        try:
            for node, st in run_agent_resume_stream(req.thread_id, req.response):
                payload: dict[str, Any] = {
                    "node": node,
                    "question_kind": st.get("question_kind"),
                    "target_companies": st.get("target_companies") or [],
                    "n_tool_results": len(st.get("tool_results") or []),
                    "cost_usd": float(st.get("llm_usage_usd") or 0.0),
                    "n_replans": st.get("n_replans"),
                    "validation_status": st.get("validation_status"),
                }
                if node == "__interrupt__":
                    payload["pending_interrupt"] = st.get("pending_interrupt") or {}
                if node == "__final__":
                    payload["answer"] = st.get("answer", "")
                    payload["citations"] = st.get("citations") or []
                    payload["grounding"] = st.get("grounding") or {}
                    payload["validation_issues"] = st.get("validation_issues") or []
                    _persist_turn(req.thread_id, "assistant", payload["answer"],
                                   citations=payload["citations"],
                                   trace={"question_kind": payload["question_kind"],
                                          "target_companies": payload["target_companies"],
                                          "cost_usd": payload["cost_usd"],
                                          "validation_status": payload["validation_status"],
                                          "resumed_from": "interrupt"})
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except RuntimeError as exc:
            err = {"node": "__error__",
                   "error": f"resume_unavailable: {exc}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:   # noqa: BLE001
            log.exception("[chat_resume] failed")
            err = {"node": "__error__", "error": f"{type(exc).__name__}: {exc}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── threads endpoints ───────────────────────────────────────
@app.get("/threads/{thread_id}")
def get_thread(thread_id: str) -> dict:
    """대화 히스토리 조회."""
    messages = _load_history(thread_id, limit=200)
    return {"thread_id": thread_id, "messages": messages}


@app.get("/health")
def health() -> dict:
    """간단 헬스 — PG/Neo4j ping."""
    out: dict = {"api": "ok"}
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        out["postgres"] = "ok"
    except Exception as e:
        out["postgres"] = f"error: {e}"
    try:
        from ..db.neo4j import get_driver
        with get_driver().session() as s:
            s.run("RETURN 1").consume()
        out["neo4j"] = "ok"
    except Exception as e:
        out["neo4j"] = f"error: {e}"
    return out


# ── persistence ─────────────────────────────────────────────
def _load_history(thread_id: str, limit: int = 20) -> list[dict]:
    """이전 메시지 N 개. user/assistant 만 (system 제외)."""
    sql = """
    WITH conv AS (
      SELECT id FROM chat.conversations WHERE thread_id = %s
    )
    SELECT role, content, citations, agent_trace, created_at
      FROM chat.messages m
      JOIN conv c ON m.conversation_id = c.id
     WHERE m.role IN ('user', 'assistant')
     ORDER BY turn_idx DESC
     LIMIT %s
    """
    out: list[dict] = []
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (thread_id, limit))
        for role, content, citations, trace, created in cur.fetchall():
            out.append({"role": role, "content": content,
                         "citations": citations or [], "agent_trace": trace or {},
                         "created_at": created.isoformat() if created else None})
    return list(reversed(out))


def _persist_turn(thread_id: str, role: str, content: str,
                  citations: list | None,
                  trace: dict | None) -> None:
    """conversations + messages 적재 (없으면 conversation 생성). 멱등 + turn_idx 자동."""
    sql_conv = """
    INSERT INTO chat.conversations (thread_id)
    VALUES (%s)
    ON CONFLICT (thread_id) DO UPDATE SET updated_at = now()
    RETURNING id
    """
    sql_max_turn = "SELECT coalesce(max(turn_idx), -1) + 1 FROM chat.messages WHERE conversation_id = %s"
    sql_insert = """
    INSERT INTO chat.messages
      (conversation_id, turn_idx, role, content, citations, agent_trace)
    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
    ON CONFLICT (conversation_id, turn_idx, role) DO NOTHING
    """
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql_conv, (thread_id,))
        conv_id = cur.fetchone()[0]
        cur.execute(sql_max_turn, (conv_id,))
        next_turn = cur.fetchone()[0]
        cur.execute(sql_insert, (
            conv_id, next_turn, role, content,
            json.dumps(citations or [], ensure_ascii=False),
            json.dumps(trace or {}, ensure_ascii=False),
        ))


__all__ = ["app"]
