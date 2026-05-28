"""AutoGraph 도메인 라우팅 정책 — 룰 기반 분류 + planner DAG 패턴.

finance 의 ``autonexusgraph.agents.policy`` 와 동일한 인터페이스를 자동차 도메인에 맞춰 제공.

- ``classify_question_auto(q)`` → AutoQuestionKind
- ``select_tools_auto(kind)``    → 권장 intent 목록
- ``plan_auto_tasks(state)``     → planner_node 가 위임할 task DAG (autograph 분기)
- ``route_domain(question, hint)``→ finance / auto / cross_domain 판정
"""

from __future__ import annotations

import re
from typing import Literal


AutoQuestionKind = Literal[
    "vehicle_spec",        # 차종 제원 (SQL)
    "vehicle_recall",      # 리콜 관계 (Graph) + 텍스트 (Vector)
    "vehicle_complaint",   # 결함 신고 텍스트 (Vector)
    "supply_chain",        # 공급사·부품 관계 (Graph)
    "vehicle_narrative",   # 자유 텍스트 검색
    "vehicle_compare",     # 차종 비교 (SQL compare_vehicles)
    "unknown",
]


# ── 룰 ─────────────────────────────────────────────────────
KW_AUTO_GENERIC = (
    "차량", "차종", "자동차", "모델", "트림", "연식",
    "OEM", "vehicle", "model", "trim", "vin",
)
KW_RECALL = ("리콜", "결함", "시정조치", "recall")
KW_COMPLAINT = ("불만", "민원", "결함신고", "complaint", "issue")
KW_SUPPLY = ("부품", "공급사", "supplier", "supply", "part", "BOM", "공급망")
KW_SPEC = (
    "제원", "스펙", "엔진", "마력", "배기량", "변속기", "연비",
    "휠베이스", "전장", "전폭", "전고", "공차중량", "최고속도",
    "spec", "engine", "horsepower", "transmission",
)
KW_COMPARE = ("비교", "vs", "차이", "compare", "versus")

# Cross-Domain 트리거 — 회사 재무 + 자동차 동시 등장.
KW_FIN = ("매출", "영업이익", "재무", "주가", "시가총액", "revenue", "earnings", "지분")


def _has_any(q: str, kws) -> bool:
    return any(k in q for k in kws)


def classify_question_auto(question: str) -> AutoQuestionKind:
    """자동차 도메인 질문 유형 룰 분류 — LLM 미사용."""
    q = question or ""
    if _has_any(q, KW_RECALL):
        return "vehicle_recall"
    if _has_any(q, KW_COMPLAINT):
        return "vehicle_complaint"
    if _has_any(q, KW_SUPPLY):
        return "supply_chain"
    if _has_any(q, KW_COMPARE) and _has_any(q, KW_SPEC + KW_AUTO_GENERIC):
        return "vehicle_compare"
    if _has_any(q, KW_SPEC):
        return "vehicle_spec"
    if _has_any(q, KW_AUTO_GENERIC):
        return "vehicle_narrative"
    return "unknown"


def select_tools_auto(kind: AutoQuestionKind) -> list[str]:
    if kind == "vehicle_spec":
        return ["lookup_vehicle", "get_vehicle_info", "get_spec"]
    if kind == "vehicle_recall":
        return ["lookup_vehicle", "list_recalls_affecting", "search_documents_auto"]
    if kind == "vehicle_complaint":
        return ["lookup_vehicle", "search_documents_auto"]
    if kind == "supply_chain":
        return ["lookup_vehicle_graph", "list_components",
                "get_suppliers_of_component", "get_vehicles_using_component"]
    if kind == "vehicle_compare":
        return ["lookup_vehicle", "compare_vehicles"]
    if kind == "vehicle_narrative":
        return ["search_documents_auto", "lookup_vehicle"]
    return ["search_documents_auto"]


# ── Domain router ──────────────────────────────────────────
def route_domain(question: str, hint: str | None = None
                 ) -> Literal["finance", "auto", "cross_domain"]:
    """힌트가 명시되면 신뢰. 없으면 키워드 룰."""
    if hint:
        h = hint.strip().lower()
        if h in ("finance", "auto", "cross_domain"):
            return h  # type: ignore[return-value]
    q = question or ""
    has_auto = _has_any(q, KW_AUTO_GENERIC + KW_RECALL + KW_SUPPLY + KW_SPEC)
    has_fin = _has_any(q, KW_FIN)
    if has_auto and has_fin:
        return "cross_domain"
    if has_auto:
        return "auto"
    return "finance"


# ── planner DAG 생성 (auto 도메인) ───────────────────────────
def plan_auto_tasks(*, question: str,
                    target_vehicles: list[int] | None = None,
                    target_models: list[int] | None = None,
                    target_makes: list[str] | None = None) -> list[dict]:
    """auto 도메인 task DAG 생성. tasks 항목은 autonexusgraph.agents.dag.make_task 호환 형식."""
    from autonexusgraph.agents.dag import make_task

    kind = classify_question_auto(question)
    tasks: list[dict] = []
    tid = 0

    def _next_id(prefix: str) -> str:
        nonlocal tid
        tid += 1
        return f"a{prefix}{tid}"

    target_models = target_models or []
    target_vehicles = target_vehicles or []
    target_makes = target_makes or []

    # 1) 식별 — query 안의 자유 단어로 lookup_vehicle 한 번.
    if question:
        tasks.append(make_task(
            _next_id("sql_"), "sql", "lookup_vehicle",
            {"query": question, "limit": 5},
        ))
        lookup_id = tasks[-1]["id"]
    else:
        lookup_id = None

    if kind == "vehicle_spec":
        for vid in target_vehicles:
            tasks.append(make_task(
                _next_id("sql_"), "sql", "get_vehicle_info",
                {"variant_id": vid},
            ))
            tasks.append(make_task(
                _next_id("sql_"), "sql", "get_spec",
                {"variant_id": vid},
            ))

    elif kind == "vehicle_recall":
        for vid in target_vehicles:
            tasks.append(make_task(
                _next_id("g_"), "graph", "list_recalls_affecting",
                {"variant_id": vid, "limit": 30},
            ))
            # 리콜 전단계 조사도 함께 — 잠재적 신호 보강.
            tasks.append(make_task(
                _next_id("g_"), "graph", "list_investigations_affecting",
                {"variant_id": vid, "limit": 20},
            ))
        for mid in target_models:
            tasks.append(make_task(
                _next_id("g_"), "graph", "list_recalls_affecting",
                {"model_id": mid, "limit": 30},
            ))
            tasks.append(make_task(
                _next_id("g_"), "graph", "list_investigations_affecting",
                {"model_id": mid, "limit": 20},
            ))
        tasks.append(make_task(
            _next_id("r_"), "research", "search_documents_auto",
            {"query": question, "top_k": 6,
             "source": "nhtsa_recall"},
        ))

    elif kind == "vehicle_complaint":
        tasks.append(make_task(
            _next_id("r_"), "research", "search_documents_auto",
            {"query": question, "top_k": 8, "source": "nhtsa_complaint"},
        ))

    elif kind == "supply_chain":
        for mid in target_models:
            tasks.append(make_task(
                _next_id("g_"), "graph", "list_components",
                {"model_id": mid, "limit": 50},
            ))
        for vid in target_vehicles:
            tasks.append(make_task(
                _next_id("g_"), "graph", "list_components",
                {"variant_id": vid, "limit": 50},
            ))

    elif kind == "vehicle_compare":
        if target_vehicles:
            tasks.append(make_task(
                _next_id("sql_"), "sql", "compare_vehicles",
                {"variant_ids": target_vehicles,
                 "measure_keys": ["spec.engine.power_kw",
                                   "spec.engine.displacement_cc",
                                   "spec.dim.length_mm",
                                   "spec.weight.curb_kg"]},
            ))

    elif kind == "vehicle_narrative":
        tasks.append(make_task(
            _next_id("r_"), "research", "search_documents_auto",
            {"query": question, "top_k": 6},
        ))

    else:  # unknown
        tasks.append(make_task(
            _next_id("r_"), "research", "search_documents_auto",
            {"query": question, "top_k": 5},
        ))

    return tasks


def plan_cross_domain_tasks(*, question: str,
                             target_companies: list[str] | None = None,
                             target_makes: list[str] | None = None,
                             target_models: list[int] | None = None,
                             target_vehicles: list[int] | None = None,
                             ) -> list[dict]:
    """cross_domain — 자동차 graph ↔ bridge ↔ finance/SEC.

    - target_companies (DART corp_code) → bridge.corp_to_entity + finance get_revenue.
    - target_models 의 OEM (글로벌) → bridge.entity_to_sec_cik + get_oem_financials_sec.
    - NHTSA recall 문서 검색은 항상 포함.
    """
    from autonexusgraph.agents.dag import make_task

    tasks: list[dict] = []
    tid = 0

    def _next_id(prefix: str) -> str:
        nonlocal tid
        tid += 1
        return f"x{prefix}{tid}"

    year = _extract_year(question)

    # ── finance 측 (DART corp_code) — bridge + 매출 ──────────
    for cc in target_companies or []:
        tasks.append(make_task(
            _next_id("br_"), "sql", "bridge_corp_to_entity",
            {"corp_code": cc, "entity_type": "manufacturer"},
        ))
        tasks.append(make_task(
            _next_id("sql_"), "sql", "get_revenue",
            {"corp_code": cc, "year": year},
        ))

    # ── 자동차 측 target_models → SEC EDGAR OEM 재무 (글로벌) ──
    # model_id 자체는 manufacturer 가 아니지만, target_models 는 식별된 OEM의 model.
    # bridge_entity_to_sec_cik 를 호출해 supervisor 가 manufacturer_id 추출.
    # 실제 financials 조회는 get_oem_financials_sec — manufacturer_id 단위.
    seen_mfr: set[int] = set()
    for mid in target_models or []:
        # MVP: model_id 그대로는 manufacturer 아니지만 후속 supervisor 가 join.
        # 본 PR 에서는 target_models 가 들어오면 그 모델의 manufacturer 정보를
        # supervisor 가 lookup_vehicle 결과로부터 보강.
        seen_mfr.add(int(mid))
    for mfr_id in seen_mfr:
        tasks.append(make_task(
            _next_id("sql_"), "sql", "bridge_entity_to_sec_cik",
            {"entity_id": str(mfr_id), "entity_type": "manufacturer"},
        ))
        tasks.append(make_task(
            _next_id("sql_"), "sql", "get_oem_financials_sec",
            {"manufacturer_id": mfr_id, "fiscal_period": "FY",
             "year_min": year, "year_max": year, "limit": 10},
        ))

    # ── 자동차 문서 + 리콜 검색 (항상) ─────────────────────────
    tasks.append(make_task(
        _next_id("r_"), "research", "search_documents_auto",
        {"query": question, "top_k": 6, "source": "nhtsa_recall"},
    ))

    return tasks


def _extract_year(q: str) -> int | None:
    m = re.search(r"(20\d{2})", q)
    return int(m.group(1)) if m else None


# ── 자동차 도메인 entity 식별 (triage 보조) ─────────────────
def identify_auto_targets(state: dict, *,
                          question: str | None = None,
                          max_per_word: int = 2,
                          max_total_vehicles: int = 5,
                          max_total_models: int = 5,
                          max_total_makes: int = 5) -> None:
    """auto/cross_domain triage 보조 — question 단어 단위로 lookup_vehicle 호출.

    상위 hits 의 variant_id / model_id / mfr_name 를 ``state["target_vehicles"]`` /
    ``state["target_models"]`` / ``state["target_makes"]`` 에 in-place 채움.

    DB 미가용·tools 모듈 import 실패 등은 무음 실패 (best-effort). 매칭 0 이면 빈 리스트.

    triage_node 가 sanitize/rewrite 후의 question 을 명시적으로 넘겨야 안전 —
    state 의 question 키만 보면 sanitize 전 원문일 수 있다.
    """
    try:
        from .tools.spec import lookup_vehicle
    except Exception:   # noqa: BLE001 — DB/import 모두 graceful
        return

    q = question if question is not None else (
        state.get("question_rewritten") or state.get("question") or ""
    )
    if not q:
        return
    year = _extract_year(q)

    target_vehicles: list[int] = []
    target_models: list[int] = []
    target_makes: list[str] = []
    seen_v: set[int] = set()
    seen_m: set[int] = set()
    seen_mk: set[str] = set()

    # 단어 단위 lookup — 자유 단어 ('리콜', '사례') 는 자연스럽게 매칭 0.
    for word in q.split():
        if len(word) < 2:
            continue
        if (len(target_vehicles) >= max_total_vehicles
                and len(target_models) >= max_total_models
                and len(target_makes) >= max_total_makes):
            break
        try:
            hits = lookup_vehicle(word, year=year, limit=max_per_word)
        except Exception:   # noqa: BLE001
            continue
        for h in hits:
            vid = h.get("variant_id")
            mid = h.get("model_id")
            mfr = h.get("mfr_name")
            if vid is not None:
                try:
                    vid_i = int(vid)
                except (TypeError, ValueError):
                    vid_i = None
                if vid_i is not None and vid_i not in seen_v and len(target_vehicles) < max_total_vehicles:
                    target_vehicles.append(vid_i)
                    seen_v.add(vid_i)
            if mid is not None:
                try:
                    mid_i = int(mid)
                except (TypeError, ValueError):
                    mid_i = None
                if mid_i is not None and mid_i not in seen_m and len(target_models) < max_total_models:
                    target_models.append(mid_i)
                    seen_m.add(mid_i)
            if mfr:
                mfr_s = str(mfr)
                if mfr_s not in seen_mk and len(target_makes) < max_total_makes:
                    target_makes.append(mfr_s)
                    seen_mk.add(mfr_s)

    state["target_vehicles"] = target_vehicles
    state["target_models"]   = target_models
    state["target_makes"]    = target_makes


__all__ = [
    "AutoQuestionKind",
    "classify_question_auto",
    "select_tools_auto",
    "route_domain",
    "plan_auto_tasks",
    "plan_cross_domain_tasks",
    "identify_auto_targets",
]
