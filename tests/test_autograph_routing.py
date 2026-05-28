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


# ── 6. _init_state ──────────────────────────────────────
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
