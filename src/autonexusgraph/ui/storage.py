"""Streamlit ↔ PG chat schema 어댑터.

api/main.py 의 _persist_turn / _load_history 와 같은 로직이지만 UI 가 직접 호출.
session_state 기반 thread_id 관리.
"""

from __future__ import annotations

import json
import uuid
from typing import Any


def get_or_create_thread_id() -> str:
    """Streamlit session 단위 thread_id. 새 대화 시 reset 호출."""
    import streamlit as st
    if "thread_id" not in st.session_state:
        st.session_state.thread_id = f"web-{uuid.uuid4().hex[:12]}"
    return st.session_state.thread_id


def reset_thread() -> None:
    import streamlit as st
    st.session_state.thread_id = f"web-{uuid.uuid4().hex[:12]}"
    st.session_state.messages = []
    st.session_state.cumulative_cost_usd = 0.0


def load_history(thread_id: str, limit: int = 20) -> list[dict]:
    """이전 user/assistant turn (PG chat.messages 직접 조회). message_id 도 반환."""
    from ..db.postgres import get_pool

    sql = """
    WITH conv AS (
      SELECT id FROM chat.conversations WHERE thread_id = %s
    )
    SELECT m.id, role, content, citations, agent_trace, created_at
      FROM chat.messages m
      JOIN conv c ON m.conversation_id = c.id
     WHERE m.role IN ('user', 'assistant')
     ORDER BY turn_idx DESC
     LIMIT %s
    """
    out: list[dict] = []
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (thread_id, limit))
            for mid, role, content, citations, trace, created in cur.fetchall():
                out.append({
                    "id": mid,
                    "role": role,
                    "content": content,
                    "citations": citations or [],
                    "agent_trace": trace or {},
                    "created_at": created.isoformat() if created else None,
                })
    except Exception:
        return []
    return list(reversed(out))


def persist_turn(
    thread_id: str,
    role: str,
    content: str,
    *,
    citations: list[dict] | None = None,
    agent_trace: dict[str, Any] | None = None,
) -> int | None:
    """PG chat.conversations + chat.messages 멱등 적재. inserted message_id 반환 (없으면 None)."""
    from ..db.postgres import get_pool

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
    RETURNING id
    """
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql_conv, (thread_id,))
            conv_id = cur.fetchone()[0]
            cur.execute(sql_max_turn, (conv_id,))
            next_turn = cur.fetchone()[0]
            cur.execute(sql_insert, (
                conv_id, next_turn, role, content,
                json.dumps(citations or [], ensure_ascii=False),
                json.dumps(agent_trace or {}, ensure_ascii=False),
            ))
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception:
        # UI 는 DB 적재 실패해도 화면은 보여야 함
        return None


def set_conversation_title(thread_id: str, title: str) -> None:
    """conversation title 1회 갱신. 기본값('New conversation') 일 때만 덮어씀."""
    from ..db.postgres import get_pool
    sql = """
    UPDATE chat.conversations
       SET title = %s, updated_at = now()
     WHERE thread_id = %s
       AND (title IS NULL OR title = 'New conversation' OR title = '')
    """
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (title[:200], thread_id))
    except Exception:
        pass


def get_conversation_title(thread_id: str) -> str | None:
    from ..db.postgres import get_pool
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT title FROM chat.conversations WHERE thread_id = %s",
                (thread_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def generate_title_from_question(question: str) -> str:
    """첫 질문에서 5단어 이내 한국어 title 생성. LLM 미가용시 룰 폴백.

    PRD §7.6.3 — title 은 첫 user message 의 첫 LLM 호출로 자동 요약 생성.
    LLM 호출 실패는 fail-soft (질문 첫 30자 사용).
    """
    fallback = (question or "").strip().splitlines()[0][:30] or "New conversation"
    if not question or len(question) < 5:
        return fallback
    try:
        from ..llm.base import get_llm_client
        from ..llm.budget_aware import budget_aware_client
        from ..llm.cost_tracker import BudgetExceeded

        client = budget_aware_client(
            get_llm_client(role="titler"),
            caller="title_summary",
            hard_limit=0.02,
        )
        resp = client.chat(
            [
                {"role": "system", "content": (
                    "사용자 질문을 5단어 이내 한국어 제목으로 요약하라. "
                    "따옴표·문장부호·번호 prefix 없이 한 줄."
                )},
                {"role": "user", "content": question},
            ],
            temperature=0.0,
            max_tokens=30,
            purpose="title",
        )
        title = (resp.content or "").strip().splitlines()[0].strip()
        import re
        title = re.sub(r'^[\'"\d\.\s\-]+', "", title)[:50]
        return title or fallback
    except BudgetExceeded:
        return fallback
    except Exception:
        return fallback


def record_feedback(message_id: int, rating: int, comment: str | None = None) -> bool:
    """+1/-1/0(comment-only) 피드백 적재 — UPSERT.

    chat.messages.id (BIGINT) 가 필요하다 → load_history 가 id 도 같이 반환하도록 확장.
    """
    from ..db.postgres import get_pool
    sql = """
    INSERT INTO chat.feedback (message_id, rating, comment)
    VALUES (%s, %s, %s)
    ON CONFLICT (message_id) DO UPDATE
      SET rating = EXCLUDED.rating,
          comment = EXCLUDED.comment,
          created_at = now()
    """
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (message_id, int(rating), comment))
        return True
    except Exception:
        return False


def list_recent_threads(limit: int = 10) -> list[dict]:
    """사이드바용 — 최근 대화 목록."""
    from ..db.postgres import get_pool
    sql = """
    SELECT c.thread_id, c.title, c.updated_at, count(m.id) AS n_messages
      FROM chat.conversations c
      LEFT JOIN chat.messages m ON m.conversation_id = c.id
     GROUP BY c.thread_id, c.title, c.updated_at
     ORDER BY c.updated_at DESC
     LIMIT %s
    """
    try:
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (limit,))
            return [
                {"thread_id": tid, "title": title,
                 "updated_at": ts.isoformat() if ts else None, "n_messages": n}
                for tid, title, ts, n in cur.fetchall()
            ]
    except Exception:
        return []
