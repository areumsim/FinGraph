"""세션 entity 메모리 단위 테스트."""

from __future__ import annotations

import time

from autonexusgraph.agents import session


def setup_function(_):
    """각 테스트 전 세션 초기화."""
    session.clear()


def test_get_returns_none_when_missing():
    assert session.get("missing-thread") is None


def test_update_creates_and_returns_snapshot():
    st = session.update("t1", target_companies=["00126380"], last_year=2024,
                        last_question_kind="factual", last_question="삼성전자 2024년 매출")
    assert st is not None
    assert st.target_companies == ["00126380"]
    assert st.last_year == 2024


def test_get_returns_snapshot_not_reference():
    """반환값을 변경해도 내부 상태에 영향이 없어야 한다 (race-free)."""
    session.update("t1", target_companies=["A", "B"])
    st1 = session.get("t1")
    st1.target_companies.append("X")
    st2 = session.get("t1")
    assert st2.target_companies == ["A", "B"]


def test_update_preserves_when_arg_is_none():
    """None / 빈 인자는 기존 값 유지."""
    session.update("t1", target_companies=["A"], last_year=2024)
    session.update("t1", last_question_kind="factual")
    st = session.get("t1")
    assert st.target_companies == ["A"]
    assert st.last_year == 2024
    assert st.last_question_kind == "factual"


def test_ttl_expiry(monkeypatch):
    """TTL 초과 시 None 반환 + 자동 제거."""
    monkeypatch.setattr(session, "_TTL_SECONDS", 0)   # 즉시 만료
    session.update("t1", target_companies=["X"])
    time.sleep(0.01)
    assert session.get("t1") is None


def test_lru_evicts_oldest(monkeypatch):
    """MAX 초과 시 가장 오래된 세션부터 제거."""
    monkeypatch.setattr(session, "_MAX_SESSIONS", 2)
    session.update("a", target_companies=["A"])
    time.sleep(0.01)
    session.update("b", target_companies=["B"])
    time.sleep(0.01)
    session.update("c", target_companies=["C"])
    # 트리거 evict (any update)
    session.update("d", target_companies=["D"])
    # 가장 오래된 a, b 중 하나 이상은 제거됐어야 함
    assert session.get("d") is not None
    remaining = sum(1 for sid in ("a", "b", "c", "d") if session.get(sid) is not None)
    assert remaining <= 2


def test_summarize_empty():
    assert session.summarize(None) == ""
    assert session.summarize(session.SessionState()) == ""


def test_summarize_includes_companies_and_year():
    st = session.update("t1", target_companies=["00126380", "00164779"],
                        last_year=2024)
    s = session.summarize(st)
    assert "00126380" in s
    assert "year=2024" in s


def test_clear_specific():
    session.update("a", target_companies=["A"])
    session.update("b", target_companies=["B"])
    session.clear("a")
    assert session.get("a") is None
    assert session.get("b") is not None


def test_clear_all():
    session.update("a", target_companies=["A"])
    session.update("b", target_companies=["B"])
    session.clear()
    assert session.get("a") is None
    assert session.get("b") is None


def test_empty_thread_id_is_noop():
    assert session.update("", target_companies=["X"]) is None
    assert session.get("") is None
