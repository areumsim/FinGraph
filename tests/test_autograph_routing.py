"""AutoGraph 도메인 라우팅 + planner + cypher 템플릿 병합 단위 테스트.

회귀 확인:
- finance 도메인 plan/route 가 그대로 동작
- auto 도메인은 autograph.policy 로 위임
- AUTO_TEMPLATES 가 finance TEMPLATES 에 자동 병합
- worker 의 도메인별 허용 intent / toolbox 라우팅
"""

from __future__ import annotations

import pytest


# ── 1. cypher_templates 자동 병합 ─────────────────────────
def test_auto_templates_merged_into_finance_registry():
    # autograph.tools import 시 autonexusgraph.tools.cypher_templates.TEMPLATES 에 병합돼야 함.
    import autograph.tools  # noqa: F401 — side-effect 가 핵심
    from autonexusgraph.tools.cypher_templates import TEMPLATES

    assert "auto_lookup_vehicle" in TEMPLATES
    assert "auto_recalls_by_variant" in TEMPLATES
    assert "auto_recalls_by_model" in TEMPLATES
    # finance 키는 손상 없이 남아 있어야
    assert "lookup_company" in TEMPLATES
    assert "list_subsidiaries" in TEMPLATES


def test_auto_template_param_validation():
    import autograph.tools  # noqa: F401
    from autonexusgraph.tools.cypher_templates import TemplateError, render_template

    # 정상 — int variant_id + limit
    cypher, bind = render_template("auto_recalls_by_variant",
                                    {"variant_id": 1, "limit": 10})
    assert "Recall" in cypher
    assert bind["variant_id"] == 1

    # 범위 위반 (limit > 500) → TemplateError
    with pytest.raises(TemplateError):
        render_template("auto_recalls_by_variant",
                        {"variant_id": 1, "limit": 9999})


# ── 2. route_domain ─────────────────────────────────────
def test_route_domain_keywords():
    from autograph.policy import route_domain
    assert route_domain("삼성전자 2024년 매출은?") == "finance"
    assert route_domain("현대 그랜저 2024 변속기는?") == "auto"
    assert route_domain("Tesla Model Y 리콜 사례") == "auto"
    # cross-domain — 자동차 키워드 + 재무 키워드 동시
    assert route_domain("현대자동차 매출과 그랜저 리콜의 관계는?") == "cross_domain"
    # hint 가 명시되면 hint 우선
    assert route_domain("아무 질문", hint="auto") == "auto"
    assert route_domain("아무 질문", hint="cross_domain") == "cross_domain"


def test_classify_question_auto():
    from autograph.policy import classify_question_auto
    assert classify_question_auto("Tesla Model Y 2023 리콜") == "vehicle_recall"
    # "결함"은 KW_RECALL 우선. complaint 케이스는 "불만/민원" 키워드로 명시.
    assert classify_question_auto("현대 코나 사용자 불만") == "vehicle_complaint"
    assert classify_question_auto("Hyundai Sonata 부품 공급사") == "supply_chain"
    assert classify_question_auto("Genesis G80 배기량 비교 BMW") == "vehicle_compare"
    assert classify_question_auto("Genesis G80 배기량") == "vehicle_spec"
    assert classify_question_auto("BMW M3 차량 정보") == "vehicle_narrative"
    assert classify_question_auto("아무 말") == "unknown"


# ── 3. plan_auto_tasks ───────────────────────────────────
def test_plan_auto_tasks_recall_produces_graph_and_research():
    from autograph.policy import plan_auto_tasks
    tasks = plan_auto_tasks(
        question="Tesla Model Y 2023 리콜",
        target_vehicles=[101],
        target_models=[],
    )
    intents = {t["intent"] for t in tasks}
    assert "lookup_vehicle" in intents      # SQL 식별
    assert "list_recalls_affecting" in intents
    assert "search_documents_auto" in intents


def test_plan_auto_tasks_spec_produces_sql_only():
    from autograph.policy import plan_auto_tasks
    tasks = plan_auto_tasks(
        question="Genesis G80 2024 엔진 배기량",
        target_vehicles=[202],
    )
    sql_tasks = [t for t in tasks if t["agent"] == "sql"]
    assert any(t["intent"] == "get_spec" for t in sql_tasks)
    assert any(t["intent"] == "get_vehicle_info" for t in sql_tasks)


def test_plan_cross_domain_includes_finance_sql_and_auto_research():
    from autograph.policy import plan_cross_domain_tasks
    tasks = plan_cross_domain_tasks(
        question="현대자동차 2024 매출과 그랜저 리콜",
        target_companies=["00164742"],
    )
    intents = {t["intent"] for t in tasks}
    assert "search_documents_auto" in intents
    assert "get_revenue" in intents
    assert "bridge_corp_to_entity" in intents


# ── 4. planner_node 위임 (도메인 분기) ───────────────────
def test_planner_node_routes_to_auto_branch():
    from autonexusgraph.agents.nodes import planner_node

    state = {
        "question": "Hyundai Sonata 2024 리콜",
        "question_rewritten": "Hyundai Sonata 2024 리콜",
        "domain": "auto",
        "target_companies": [],
        "target_vehicles": [],
        "target_models": [],
        "llm_usage_usd": 0.0,
        "n_replans": 0,
    }
    out = planner_node(state)
    assert out.get("tasks"), "auto planner 가 tasks 생성해야 함"
    intents = {t["intent"] for t in out["tasks"]}
    # auto recall 분기는 search_documents_auto 포함
    assert "search_documents_auto" in intents


def test_planner_node_finance_unaffected_when_no_domain():
    """domain 미지정 시 router 가 finance 로 판정 → 기존 finance planner 분기."""
    from autonexusgraph.agents.nodes import planner_node

    state = {
        "question": "삼성전자 2024년 매출",
        "question_rewritten": "삼성전자 2024년 매출",
        "domain": "finance",
        "question_kind": "factual",
        "target_companies": ["00126380"],
        "llm_usage_usd": 0.0,
        "n_replans": 0,
    }
    out = planner_node(state)
    sql_tasks = [t for t in out["tasks"] if t["agent"] == "sql"]
    # finance factual 패턴 — get_revenue + get_operating_income
    assert {t["intent"] for t in sql_tasks} == {"get_revenue", "get_operating_income"}


# ── 5. workers 도메인 분기 ─────────────────────────────────
def test_graph_worker_rejects_finance_intent_in_auto_domain():
    """auto 도메인에서 finance graph intent (list_subsidiaries) 는 거절돼야."""
    from autonexusgraph.agents.workers import graph_worker
    state = {"domain": "auto", "tasks": [], "task_results": {}, "tool_results": []}
    task = {"id": "t1", "agent": "graph", "intent": "list_subsidiaries",
            "args": {}, "depends_on": [], "status": "pending", "result": None}
    out = graph_worker(state, task)
    # task.status == 'skipped' (intent 미허용)
    assert out["tool_results"][-1]["status"] == "skipped"


def test_sql_worker_accepts_lookup_vehicle_in_auto_domain():
    """auto 도메인에서 sql intent 'lookup_vehicle' 이 허용목록에 있는지만 검증 (실호출은 mock)."""
    from autonexusgraph.agents import workers as W
    state = {"domain": "auto", "tasks": [], "task_results": {}, "tool_results": []}
    allowed = W._allowed_intents(state, "sql")
    assert "lookup_vehicle" in allowed
    assert "get_spec" in allowed
    assert "compare_vehicles" in allowed
    # finance intent 는 auto 단독 도메인에서는 없어야
    assert "get_revenue" not in allowed


def test_cross_domain_allowed_includes_both():
    from autonexusgraph.agents import workers as W
    state = {"domain": "cross_domain"}
    sql_allowed = W._allowed_intents(state, "sql")
    assert "get_revenue" in sql_allowed
    assert "get_spec" in sql_allowed
    research_allowed = W._allowed_intents(state, "research")
    assert "search_documents" in research_allowed
    assert "search_documents_auto" in research_allowed


# ── 6. identify_auto_targets / triage 통합 (B17 fix) ──────
def test_identify_auto_targets_populates_state(monkeypatch):
    """단어 단위 lookup_vehicle 결과를 state 의 target_* 에 누적."""
    from autograph import policy as p
    from autograph.tools import spec

    calls: list[str] = []

    def fake_lookup(query, *, year=None, limit=5):
        calls.append(query)
        if query == "Tesla":
            return [{"variant_id": 101, "model_id": 51,
                     "mfr_name": "Tesla", "model_year": 2023}]
        if query == "Model":
            return [{"variant_id": 102, "model_id": 51,
                     "mfr_name": "Tesla", "model_year": 2023}]
        return []

    monkeypatch.setattr(spec, "lookup_vehicle", fake_lookup)

    state = {"question": "Tesla Model Y 2023 리콜"}
    p.identify_auto_targets(state, question=state["question"])

    assert 101 in state["target_vehicles"]
    assert 102 in state["target_vehicles"]
    assert state["target_models"] == [51]
    assert state["target_makes"] == ["Tesla"]
    # 'Y' 는 길이 1 → skip. 자유 단어는 빈 hits.
    assert "Y" not in calls


def test_identify_auto_targets_empty_question_noop(monkeypatch):
    from autograph import policy as p
    from autograph.tools import spec

    def boom(*a, **kw):
        raise AssertionError("lookup_vehicle 호출되면 안 됨")
    monkeypatch.setattr(spec, "lookup_vehicle", boom)
    state: dict = {}
    p.identify_auto_targets(state, question="")
    assert state == {}


def test_identify_auto_targets_swallows_db_error(monkeypatch):
    from autograph import policy as p
    from autograph.tools import spec

    def fail(*a, **kw):
        raise RuntimeError("postgres down")
    monkeypatch.setattr(spec, "lookup_vehicle", fail)

    state = {"question": "Hyundai Sonata 2024"}
    # 예외 누출 없이 빈 결과로 끝나야 함.
    p.identify_auto_targets(state, question=state["question"])
    assert state["target_vehicles"] == []
    assert state["target_models"] == []
    assert state["target_makes"] == []


def test_triage_node_populates_auto_targets_in_auto_domain(monkeypatch):
    """domain=auto 인 triage_node 가 identify_auto_targets 를 호출하는지."""
    from autonexusgraph.agents import nodes as N
    from autonexusgraph.tools import financials as fin
    from autograph.tools import spec
    from autonexusgraph.agents import session

    # finance lookup_company — 무관, 빈 결과 반환.
    monkeypatch.setattr(fin, "lookup_company", lambda *a, **kw: [])

    def fake_auto(query, *, year=None, limit=5):
        if query == "Hyundai":
            return [{"variant_id": 11, "model_id": 7,
                     "mfr_name": "Hyundai", "model_year": 2024}]
        if query == "Sonata":
            return [{"variant_id": 12, "model_id": 7,
                     "mfr_name": "Hyundai", "model_year": 2024}]
        return []
    monkeypatch.setattr(spec, "lookup_vehicle", fake_auto)

    # session in-memory clear (이전 테스트 영향 차단)
    session.clear()

    state = {
        "question": "Hyundai Sonata 2024 리콜",
        "domain": "auto",
        "history": [],
        "llm_usage_usd": 0.0,
        "thread_id": "test-auto-1",
    }
    out = N.triage_node(state)
    assert 11 in (out.get("target_vehicles") or [])
    assert 12 in (out.get("target_vehicles") or [])
    assert 7 in (out.get("target_models") or [])
    assert "Hyundai" in (out.get("target_makes") or [])


def test_triage_finance_domain_skips_auto_lookup(monkeypatch):
    """domain=finance 일 때 identify_auto_targets 가 호출되지 않아야."""
    from autonexusgraph.agents import nodes as N
    from autonexusgraph.tools import financials as fin
    from autograph import policy as p
    from autonexusgraph.agents import session

    monkeypatch.setattr(fin, "lookup_company", lambda *a, **kw: [])

    calls = []
    def trap(*a, **kw):
        calls.append(1)
    monkeypatch.setattr(p, "identify_auto_targets", trap)

    session.clear()
    state = {
        "question": "삼성전자 2024년 매출",
        "domain": "finance",
        "history": [],
        "llm_usage_usd": 0.0,
        "thread_id": "test-fin-1",
    }
    N.triage_node(state)
    assert calls == [], "finance 도메인에서 auto identify 호출되면 안 됨"


def test_triage_then_planner_auto_recall_produces_graph_tasks(monkeypatch):
    """triage 가 target_vehicles 채운 뒤 planner 가 list_recalls_affecting task 생성."""
    from autonexusgraph.agents import nodes as N
    from autonexusgraph.tools import financials as fin
    from autograph.tools import spec
    from autonexusgraph.agents import session

    monkeypatch.setattr(fin, "lookup_company", lambda *a, **kw: [])
    monkeypatch.setattr(spec, "lookup_vehicle",
        lambda q, **kw: [{"variant_id": 42, "model_id": 9,
                          "mfr_name": "Hyundai", "model_year": 2024}]
        if q == "Hyundai" else [])
    session.clear()

    state = {
        "question": "Hyundai Sonata 2024 리콜",
        "domain": "auto",
        "history": [],
        "llm_usage_usd": 0.0,
        "n_replans": 0,
        "thread_id": "test-tp-1",
    }
    state = N.triage_node(state)
    assert state.get("target_vehicles"), "triage 가 target_vehicles 채워야 함"
    state = N.planner_node(state)
    intents = [t["intent"] for t in state.get("tasks") or []]
    assert "list_recalls_affecting" in intents, (
        "target_vehicles 가 채워졌으니 graph 분기가 살아나야 함"
    )
    assert "search_documents_auto" in intents


def test_session_carryover_auto_targets(monkeypatch):
    """이전 turn 의 target_vehicles 가 다음 turn 으로 carry-over."""
    from autonexusgraph.agents import session

    session.clear()
    # 첫 turn — 식별 결과 저장.
    session.update("tid-co",
                    target_vehicles=[100, 101],
                    target_models=[55],
                    target_makes=["Kia"],
                    last_year=2024)
    st = session.get("tid-co")
    assert st is not None
    assert st.target_vehicles == [100, 101]
    assert st.target_models == [55]
    assert st.target_makes == ["Kia"]
    # 다음 turn 에서 빈 인자로 update — 기존 값 보존.
    session.update("tid-co", last_question="다음 질문")
    st2 = session.get("tid-co")
    assert st2.target_vehicles == [100, 101]


def test_triage_multi_turn_carries_over_auto_targets(monkeypatch):
    """B18 — triage_node 두 번 호출 시 두 번째 turn 이 첫 turn 의 차종을 carry-over.

    1 turn: "현대 그랜저 변속기" → lookup_vehicle 이 hit → target_vehicles=[10] 저장.
    2 turn: "그 차의 리콜은?" → lookup_vehicle 매칭 0 → carry-over 로 [10] 복원.
    """
    from autonexusgraph.agents import nodes as N
    from autonexusgraph.tools import financials as fin
    from autograph.tools import spec
    from autonexusgraph.agents import session

    monkeypatch.setattr(fin, "lookup_company", lambda *a, **kw: [])

    def fake_auto(query, *, year=None, limit=5):
        if query == "현대":
            return [{"variant_id": 10, "model_id": 3,
                     "mfr_name": "Hyundai", "model_year": 2024}]
        if query == "그랜저":
            return [{"variant_id": 11, "model_id": 3,
                     "mfr_name": "Hyundai", "model_year": 2024}]
        return []  # "그", "차의", "리콜은?" 등 — 매칭 0
    monkeypatch.setattr(spec, "lookup_vehicle", fake_auto)

    session.clear()
    tid = "test-multi-1"

    # ── 1 turn — entity 식별 성공
    s1 = {
        "question": "현대 그랜저 변속기",
        "domain": "auto",
        "history": [],
        "llm_usage_usd": 0.0,
        "thread_id": tid,
    }
    s1 = N.triage_node(s1)
    assert 10 in (s1.get("target_vehicles") or [])
    assert 11 in (s1.get("target_vehicles") or [])
    assert not s1.get("session_carryover"), "1 turn 은 carry-over 아님"

    # 세션 보존 확인.
    st = session.get(tid)
    assert st is not None and st.target_vehicles == [10, 11]

    # ── 2 turn — entity 매칭 0 → 이전 turn 의 차종 borrow
    s2 = {
        "question": "그 차의 리콜은?",
        "domain": "auto",
        "history": [{"role": "user", "content": "현대 그랜저 변속기"}],
        "llm_usage_usd": 0.0,
        "thread_id": tid,
    }
    s2 = N.triage_node(s2)
    assert s2.get("target_vehicles") == [10, 11], (
        f"carry-over 실패: {s2.get('target_vehicles')}"
    )
    assert s2.get("target_models") == [3]
    assert s2.get("target_makes") == ["Hyundai"]
    assert s2.get("session_carryover") is True


def test_triage_carryover_skipped_when_current_turn_has_targets(monkeypatch):
    """B18 — 2 turn 에서도 매칭이 있으면 carry-over 안 함 (새 값 우선)."""
    from autonexusgraph.agents import nodes as N
    from autonexusgraph.tools import financials as fin
    from autograph.tools import spec
    from autonexusgraph.agents import session

    monkeypatch.setattr(fin, "lookup_company", lambda *a, **kw: [])

    state_of_call = {"n": 0}
    def fake_auto(query, *, year=None, limit=5):
        state_of_call["n"] += 1
        if state_of_call["n"] == 1 and query == "Kia":
            return [{"variant_id": 20, "model_id": 7,
                     "mfr_name": "Kia", "model_year": 2024}]
        if state_of_call["n"] >= 2 and query == "Tesla":
            return [{"variant_id": 21, "model_id": 8,
                     "mfr_name": "Tesla", "model_year": 2024}]
        return []
    monkeypatch.setattr(spec, "lookup_vehicle", fake_auto)

    session.clear()
    tid = "test-multi-2"

    s1 = N.triage_node({
        "question": "Kia EV6",
        "domain": "auto", "history": [], "llm_usage_usd": 0.0, "thread_id": tid,
    })
    assert s1.get("target_vehicles") == [20]

    s2 = N.triage_node({
        "question": "Tesla Model 3 비교",
        "domain": "auto", "history": [], "llm_usage_usd": 0.0, "thread_id": tid,
    })
    # carry-over 가 일어나면 [20] 이 남았을 것. 새 매칭이 우선해야 함.
    assert 21 in (s2.get("target_vehicles") or [])
    assert 20 not in (s2.get("target_vehicles") or []), (
        "새 turn 에서 매칭이 있으면 이전 turn 값을 borrow 하지 말아야"
    )
    assert not s2.get("session_carryover")


# ── 7. executor 폴백 도메인 인식 (B20) ───────────────────
def test_executor_fallback_uses_search_documents_auto_for_auto_domain(monkeypatch):
    from autonexusgraph.agents import nodes as N

    fin_calls: list[dict] = []
    auto_calls: list[dict] = []

    # finance 측 retrieve — 빈 결과 (도구가 빈 결과만 반환하는 시나리오).
    def fin_sd(**kw):
        fin_calls.append(kw)
        return []
    monkeypatch.setattr("autonexusgraph.tools.search_documents", fin_sd)
    # tools 모듈에 등록된 다른 finance 도구가 호출돼도 빈 결과만 나오게 만들 필요는 없음.
    # auto fallback 만 호출되는지 확인.

    def auto_sd(query, *, top_k=8, model_id=None, **kw):
        auto_calls.append({"query": query, "top_k": top_k, "model_id": model_id, **kw})
        return [{"id": 1, "text": "auto hit"}]
    monkeypatch.setattr("autograph.tools.search_documents_auto", auto_sd)

    state = {
        "question": "Tesla Model Y 2023 리콜",
        "question_rewritten": "Tesla Model Y 2023 리콜",
        "domain": "auto",
        "target_models": [9],
        # plan 에 다른 도구도 없게 — 폴백 분기 진입 조건: results 비거나 모두 빈 결과.
        "plan": [],
        "history": [],
    }
    out = N.executor_node(state)
    assert out.get("fallback_used") is True
    assert auto_calls, "auto 도메인 폴백은 search_documents_auto 호출해야 함"
    assert not fin_calls, "auto 도메인에서 finance search_documents 호출되면 안 됨"
    assert auto_calls[0]["model_id"] == 9
    # tool_results 에 fallback_recovery 기록.
    rec = out["tool_results"][-1]
    assert rec["tool"] == "search_documents_auto"
    assert rec["purpose"] == "fallback_recovery"


def test_executor_fallback_uses_finance_search_for_finance_domain(monkeypatch):
    from autonexusgraph.agents import nodes as N

    fin_calls: list[dict] = []
    def fin_sd(**kw):
        fin_calls.append(kw)
        return [{"id": 1, "text": "fin hit"}]
    monkeypatch.setattr("autonexusgraph.tools.search_documents", fin_sd)
    # auto 가 잘못 호출되면 에러로 시그널.
    def auto_boom(*a, **kw):
        raise AssertionError("finance 도메인에서 auto fallback 호출되면 안 됨")
    monkeypatch.setattr("autograph.tools.search_documents_auto", auto_boom)

    state = {
        "question": "삼성전자 2024 매출",
        "domain": "finance",
        "target_companies": ["00126380"],
        "plan": [],
        "history": [],
    }
    out = N.executor_node(state)
    assert out.get("fallback_used") is True
    assert fin_calls and fin_calls[0]["corp_code"] == "00126380"


# ── 8. cross_query 일반화 (B22) ──────────────────────────
def test_cross_query_backward_compat(monkeypatch):
    """옛 호출 (manufacturer_id, target) 가 그대로 동작."""
    from autograph.tools import bridge as br

    monkeypatch.setattr(br, "bridge_entity_to_corp",
                         lambda eid, etype, **kw: [{"corp_code": "X", "etype": etype, "eid": eid}])
    out = br.cross_query(manufacturer_id=42)
    assert out["entity_type"] == "manufacturer"
    assert out["entity_id"] == 42
    assert out["corps"][0]["etype"] == "manufacturer"
    assert out["corps"][0]["eid"] == "42"


def test_cross_query_supplier_type(monkeypatch):
    """새 호출 — entity_type='supplier' 로 정확히 라우팅."""
    from autograph.tools import bridge as br

    captured = {}
    def fake(eid, etype, **kw):
        captured["eid"] = eid
        captured["etype"] = etype
        return [{"corp_code": "S", "etype": etype}]
    monkeypatch.setattr(br, "bridge_entity_to_corp", fake)

    out = br.cross_query(entity_id="123", entity_type="supplier")
    assert out["entity_type"] == "supplier"
    assert captured == {"eid": "123", "etype": "supplier"}


def test_cross_query_invalid_entity_type():
    from autograph.tools import bridge as br
    import pytest as _pt
    with _pt.raises(ValueError):
        br.cross_query(corp_code="X", entity_type="invalid_type")


# ── 9. _init_state ──────────────────────────────────────
def test_init_state_auto_detects_domain():
    from autonexusgraph.agents.graph import _init_state
    s_fin = _init_state("삼성전자 매출", "tid", None)
    assert s_fin["domain"] == "finance"
    s_auto = _init_state("현대 그랜저 변속기", "tid", None)
    assert s_auto["domain"] == "auto"
    s_cross = _init_state("현대자동차 매출과 그랜저 리콜", "tid", None)
    assert s_cross["domain"] == "cross_domain"
    s_hint = _init_state("아무 질문", "tid", None, domain="auto")
    assert s_hint["domain"] == "auto"
