"""에이전트 노드들 — Triage / Planner / Executor / Synthesizer.

각 노드는 AgentState → AgentState (mutation 후 return). LangGraph 도입 시 StateGraph 의
node 로 그대로 등록 가능.

cost guard 적용 원칙 (사용자 명시):
- 모든 LLM 노드 (Planner / Synthesizer 등) 는 budget_aware_client 사용.
- 노드 진입 시 turn_budget_exceeded(state) 체크 → 초과면 즉시 fallback 답변으로 점프.

현재 단계 (Phase 4 골격):
- Triage: 룰 기반 (LLM 0). policy.classify_question 만 호출.
- Planner: 룰 기반 (LLM 0) — policy.select_tools 만 호출. 향후 LLM 으로 업그레이드 가능.
- Executor: tools/ 함수 직접 호출. LLM 0.
- Synthesizer: LLM 호출 — 답변 합성.
"""

from __future__ import annotations

import logging
from typing import Any

from . import session
from .policy import classify_question, select_tools, turn_budget_exceeded
from .state import AgentState
from .temporal import normalize_temporal_terms, extract_year_hint


log = logging.getLogger(__name__)


# ── Triage ──────────────────────────────────────────────────
def triage_node(state: AgentState) -> AgentState:
    """질문 유형 분류 + 1차 회사 식별 + 상대 시간 정규화."""
    from ..tools.financials import lookup_company as lookup_pg

    from ..safety import sanitize_user_input
    from .rewriter import rewrite_query

    raw_q = state.get("question", "")
    # 1) 프롬프트 인젝션 신호 감지 + XML 경계 escape — 입력 → safety 통과 → 본 파이프라인
    safe_q, signals = sanitize_user_input(raw_q, context="agent_input")
    if signals:
        state["safety_signals"] = signals
    q = safe_q
    # 2) 멀티턴 coreference 해소 — "그 중", "위 회사들" → 이전 turn 의 entity 풀어쓰기
    history = state.get("history") or []
    if history:
        q_rew, rewrite_audit = rewrite_query(question=q, history=history)
        if rewrite_audit.get("called"):
            state["rewrite_audit"] = rewrite_audit
            q = q_rew
    # 3) 한국어 상대 시간 정규화 — '작년'/'최근 3년' → 절대 연도 (rewrite 후에 적용)
    q_norm, temporal_audit = normalize_temporal_terms(q)
    if temporal_audit.get("applied") or state.get("rewrite_audit"):
        state["question_rewritten"] = q_norm
    if temporal_audit.get("applied"):
        state["temporal_audit"] = temporal_audit
        q = q_norm
    kind = classify_question(q)
    state["question_kind"] = kind

    # 회사 식별 — 모호성 검출 + (가능 시) HITL clarification (PRD §7.5.6)
    from .interrupts import (
        InterruptUnavailable,
        coerce_clarification_response,
        is_ambiguous_company,
        make_clarification_payload,
        request_interrupt,
    )

    thread_id = state.get("thread_id") or ""
    targets: list[str] = []
    interrupt_response = state.get("interrupt_response")

    # 사용자가 이미 clarification 답을 보냈으면 그것을 우선 적용 (재개 흐름)
    if interrupt_response and (state.get("pending_interrupt") or {}).get("kind") == "company_clarification":
        cands = (state.get("pending_interrupt") or {}).get("candidates") or []
        chosen = coerce_clarification_response(interrupt_response, cands)
        if chosen:
            targets = [chosen]
            state["interrupt_handled"] = True
            state["pending_interrupt"] = {}
            log.info("[triage] clarification 응답 적용: %s", chosen)

    # 후보 추출 — word 단위로 lookup, 각 word 가 모호하면 첫 모호 지점에서 interrupt
    if not targets:
        for word in q.split():
            if len(word) < 2:
                continue
            try:
                hits = lookup_pg(word, limit=5)
            except Exception:
                hits = []
            if not hits:
                continue
            # 모호성 — 후보 ≥ 2 + score margin 작음
            if is_ambiguous_company(hits):
                payload = make_clarification_payload(
                    query=word, candidates=hits, thread_id=thread_id,
                )
                state["pending_interrupt"] = dict(payload)
                try:
                    resp = request_interrupt(payload)
                    chosen = coerce_clarification_response(resp, hits)
                    if chosen:
                        targets.append(chosen)
                        state["interrupt_handled"] = True
                        state["pending_interrupt"] = {}
                        continue
                except InterruptUnavailable:
                    # 폴백 체인 — 1순위 자동 선택 + 경고
                    cc = str(hits[0].get("corp_code") or "")
                    if cc:
                        targets.append(cc)
                        state.setdefault("safety_signals", []).append(
                            f"ambiguous_company_auto_resolved:{word}->{cc}"
                        )
                        log.warning("[triage] interrupt 미지원 — '%s' 1순위(%s) 자동 선택", word, cc)
                continue
            # 모호 X → 1순위 채택
            cc = str(hits[0].get("corp_code") or "")
            if cc and cc not in targets:
                targets.append(cc)
            if len(targets) >= 5:
                break

    # 세션 entity carry-over — 이번 turn 에 회사가 식별 안 되고 multi-turn 이면
    # 이전 세션의 target_companies/persons 를 borrow (PRD §7.6.2).
    prev = session.get(thread_id) if thread_id else None
    if not targets and prev and prev.target_companies:
        targets = list(prev.target_companies)
        state["session_carryover"] = True
        log.info("[triage] carry-over targets from session: %s", targets)

    state["target_companies"] = targets

    # ── AutoGraph 도메인 entity 식별 (B17 fix) ────────────────
    # auto / cross_domain 일 때 question 단어 단위 lookup_vehicle 로 target_vehicles /
    # target_models / target_makes 를 채운다. 이게 없으면 plan_auto_tasks 의
    # vehicle_spec/recall/supply_chain/compare 분기가 빈 루프로 끝남.
    domain = str(state.get("domain") or "finance").lower()
    if domain in ("auto", "cross_domain"):
        try:
            from autograph.policy import identify_auto_targets
            identify_auto_targets(state, question=q)
        except ImportError as exc:
            log.warning("[triage:auto] autograph 패키지 미설치 — auto 도메인 fallback 으로 finance 처리됨: %s", exc)
            state.setdefault("safety_signals", []).append(
                f"autograph_import_failed:{type(exc).__name__}"
            )
        except Exception as exc:   # noqa: BLE001
            log.warning("[triage:auto] identify failed: %s", exc)
            state.setdefault("safety_signals", []).append(
                f"auto_identify_failed:{type(exc).__name__}"
            )

        # auto 도메인 session carry-over — 이번 turn 에서 매칭 0 인데 이전 turn 에
        # 차종이 있었으면 borrow (PRD §7.6.2 multi-turn 보존).
        # prev 는 finance 분기에서 이미 session.get 으로 한 번 가져왔음 (재호출 불필요).
        if prev:
            if not (state.get("target_vehicles") or []) and prev.target_vehicles:
                state["target_vehicles"] = list(prev.target_vehicles)
                state["session_carryover"] = True
                log.info("[triage:auto] carry-over target_vehicles: %s",
                         state["target_vehicles"])
            if not (state.get("target_models") or []) and prev.target_models:
                state["target_models"] = list(prev.target_models)
                state["session_carryover"] = True
            if not (state.get("target_makes") or []) and prev.target_makes:
                state["target_makes"] = list(prev.target_makes)

    # 이번 turn 결과를 세션에 기록 (다음 turn 의 carry-over 재료)
    if thread_id:
        year_hint = extract_year_hint(state.get("question_rewritten") or q)
        session.update(
            thread_id,
            target_companies=targets if targets else None,
            target_vehicles=state.get("target_vehicles") or None,
            target_models=state.get("target_models") or None,
            target_makes=state.get("target_makes") or None,
            last_year=year_hint,
            last_question_kind=kind,
            last_question=q,
        )

    log.info("[triage] kind=%s targets=%s vehicles=%s",
             kind, targets, state.get("target_vehicles") or [])
    return state


# ── Planner ─────────────────────────────────────────────────
def planner_node(state: AgentState) -> AgentState:
    """질문 유형 + 회사 → task DAG (PRD §7.5.2 / §7.5.3).

    룰 기반 1차 구현 (LLM upgrade 는 별도 PR). question_kind 별 패턴:
      factual    : SQL 단발 (get_revenue/get_op) — 회사 수만큼 병렬
      structural : Graph 다발 (list_subsidiaries/get_executives/get_major_shareholders) 병렬
      narrative  : Research 단발
      multi_hop  : Graph + SQL + Research 조합 — SQL 은 Graph 결과에 의존
      unknown    : Research 단발 안전 default

    여전히 ``state["plan"]`` (flat list) 도 채워서 executor 폴백 호환.
    """
    from .dag import make_task

    kind = state.get("question_kind") or "unknown"
    targets = state.get("target_companies") or []
    year_hint = extract_year_hint(state.get("question_rewritten") or state.get("question", ""))
    q = state.get("question_rewritten") or state.get("question", "")

    # ── 도메인 분기 — auto / cross_domain 은 autograph.policy 로 위임 ──
    domain = str(state.get("domain") or "finance").lower()
    if domain == "auto":
        try:
            from autograph.policy import plan_auto_tasks
            tasks = plan_auto_tasks(
                question=q,
                target_vehicles=state.get("target_vehicles") or [],
                target_models=state.get("target_models") or [],
                target_makes=state.get("target_makes") or [],
            )
            state["tasks"] = tasks
            state["task_results"] = {}
            state["plan"] = [{"tool": t["intent"], "args": t["args"],
                              "purpose": f"{t['agent']}:{t['intent']}"} for t in tasks]
            log.info("[planner:auto] tasks=%d", len(tasks))
        except ImportError as exc:
            log.warning("[planner:auto] autograph 패키지 미설치: %s", exc)
            state.setdefault("safety_signals", []).append(
                f"autograph_import_failed:{type(exc).__name__}"
            )
            state["tasks"] = []
            state["plan"] = []
        except Exception as exc:  # noqa: BLE001
            log.warning("[planner:auto] failed — fallback to research: %s", exc)
            state.setdefault("safety_signals", []).append(
                f"auto_plan_failed:{type(exc).__name__}"
            )
            state["tasks"] = []
            state["plan"] = []
        return _planner_cost_gate(state, kind, targets, len(state.get("tasks") or []))
    if domain == "cross_domain":
        try:
            from autograph.policy import plan_cross_domain_tasks
            tasks = plan_cross_domain_tasks(
                question=q,
                target_companies=targets,
                target_makes=state.get("target_makes") or [],
                target_models=state.get("target_models") or [],
                target_vehicles=state.get("target_vehicles") or [],
            )
            state["tasks"] = tasks
            state["task_results"] = {}
            state["plan"] = [{"tool": t["intent"], "args": t["args"],
                              "purpose": f"{t['agent']}:{t['intent']}"} for t in tasks]
            log.info("[planner:cross_domain] tasks=%d", len(tasks))
        except ImportError as exc:
            log.warning("[planner:cross_domain] autograph 패키지 미설치: %s", exc)
            state.setdefault("safety_signals", []).append(
                f"autograph_import_failed:{type(exc).__name__}"
            )
            state["tasks"] = []
            state["plan"] = []
        except Exception as exc:  # noqa: BLE001
            log.warning("[planner:cross_domain] failed: %s", exc)
            state.setdefault("safety_signals", []).append(
                f"cross_domain_plan_failed:{type(exc).__name__}"
            )
            state["tasks"] = []
            state["plan"] = []
        return _planner_cost_gate(state, kind, targets, len(state.get("tasks") or []))

    tasks: list[dict] = []
    tid = 0

    def _next_id(prefix: str) -> str:
        nonlocal tid
        tid += 1
        return f"{prefix}{tid}"

    # ── factual: SQL ────────────────────────────────────────
    if kind == "factual":
        for cc in targets:
            tasks.append(make_task(
                _next_id("sql_"), "sql", "get_revenue",
                {"corp_code": cc, "year": year_hint},
            ))
            tasks.append(make_task(
                _next_id("sql_"), "sql", "get_operating_income",
                {"corp_code": cc, "year": year_hint},
            ))

    # ── structural: Graph 다발 (회사별 병렬) ────────────────
    elif kind == "structural":
        for cc in targets:
            tasks.append(make_task(
                _next_id("g_"), "graph", "list_subsidiaries",
                {"parent_corp_code": cc, "limit": 20},
            ))
            tasks.append(make_task(
                _next_id("g_"), "graph", "get_executives",
                {"corp_code": cc, "limit": 30},
            ))
            tasks.append(make_task(
                _next_id("g_"), "graph", "get_major_shareholders",
                {"corp_code": cc, "limit": 10},
            ))

    # ── narrative: Research ────────────────────────────────
    elif kind == "narrative":
        if q:
            tasks.append(make_task(
                _next_id("r_"), "research", "search_documents",
                {
                    "query": q, "top_k": 6,
                    "corp_code": targets[0] if len(targets) == 1 else (targets or None),
                    "fiscal_year": year_hint,
                },
            ))

    # ── multi_hop: Graph 먼저 → SQL 집계 → Research 보완 ───
    elif kind == "multi_hop":
        graph_ids: list[str] = []
        for cc in targets:
            gid = _next_id("g_")
            graph_ids.append(gid)
            tasks.append(make_task(
                gid, "graph", "list_subsidiaries",
                {"parent_corp_code": cc, "limit": 30},
            ))
        for cc in targets:
            # SQL 은 Graph 결과를 보고 corp_code 확정 — 의존성 표현
            tasks.append(make_task(
                _next_id("sql_"), "sql", "get_revenue",
                {"corp_code": cc, "year": year_hint},
                depends_on=graph_ids,
            ))
        if q:
            tasks.append(make_task(
                _next_id("r_"), "research", "search_documents",
                {
                    "query": q, "top_k": 6,
                    "corp_code": targets[0] if len(targets) == 1 else (targets or None),
                    "fiscal_year": year_hint,
                },
            ))

    # ── unknown: 안전 default — research ────────────────────
    else:
        if q:
            tasks.append(make_task(
                _next_id("r_"), "research", "search_documents",
                {"query": q, "top_k": 6,
                 "corp_code": targets[0] if len(targets) == 1 else (targets or None)},
            ))

    state["tasks"] = tasks
    state["task_results"] = {}

    # 호환용 legacy plan — executor 폴백 (tasks 빈 경우 사용)
    plan: list[dict] = []
    for t in tasks:
        plan.append({
            "tool": t["intent"],
            "args": t["args"],
            "purpose": f"{t['agent']}:{t['intent']}",
        })
    state["plan"] = plan

    log.info("[planner] kind=%s targets=%d tasks=%d (DAG)",
             kind, len(targets), len(tasks))

    return _planner_cost_gate(state, kind, targets, len(tasks))


def _handle_cost_resume(state: "AgentState") -> bool:
    """이미 보낸 cost_approval 의 사용자 응답을 처리.

    Returns:
        True  — 응답 처리됨 (turn 진행 또는 중단 결정 완료)
        False — 보낸 적이 없거나 응답 미수신
    """
    from .interrupts import coerce_cost_response

    pi = state.get("pending_interrupt") or {}
    resp = state.get("interrupt_response")
    if not (pi.get("kind") == "cost_approval"
            and resp is not None
            and not state.get("interrupt_handled")):
        return False

    approved = coerce_cost_response(resp)
    state["interrupt_handled"] = True
    state["pending_interrupt"] = {}
    if not approved:
        state["aborted_reason"] = "cost_rejected"
        log.info("[planner] cost_approval 거절 — turn 종료")
    else:
        log.info("[planner] cost_approval 승인 (resume) — 진행")
    return True


def _request_cost_approval(state: "AgentState", kind: str, targets: list,
                            n_tasks: int, domain: str) -> None:
    """새로운 cost approval 요청 — replan 첫 turn 일 때만."""
    from .cost_estimator import needs_cost_approval
    from .interrupts import (
        InterruptUnavailable,
        coerce_cost_response,
        make_cost_approval_payload,
        request_interrupt,
    )

    need, est = needs_cost_approval(state)
    if not need:
        return

    summary = (
        f"도메인: {domain}, 질문 유형: {kind}, 대상: {len(targets)}, "
        f"task: {n_tasks}개, 모델: {est.model} "
        f"(replan 최대 {est.replan_factor}회 포함)"
    )
    payload = make_cost_approval_payload(
        estimated_cost_usd=est.estimated_cost_usd,
        plan_summary=summary,
        thread_id=state.get("thread_id") or "",
    )
    state["pending_interrupt"] = dict(payload)
    try:
        approved = coerce_cost_response(request_interrupt(payload))
        state["interrupt_handled"] = True
        state["pending_interrupt"] = {}
        if not approved:
            state["aborted_reason"] = "cost_rejected"
            log.info("[planner] cost_approval 거절 — turn 종료")
    except InterruptUnavailable:
        state.setdefault("safety_signals", []).append(
            f"cost_approval_auto_passed:${est.estimated_cost_usd:.4f}"
        )
        log.warning("[planner] interrupt 미지원 — 추정 비용 $%.4f 자동 통과",
                    est.estimated_cost_usd)


def _planner_cost_gate(state: "AgentState", kind: str, targets: list,
                       n_tasks: int) -> "AgentState":
    """planner 의 cost-approval 게이트 — finance/auto/cross_domain 모두 공통.

    PRD §7.5.6 HITL. replan 중이거나 이미 승인된 turn 은 skip.
    """
    domain = str(state.get("domain") or "finance")

    if _handle_cost_resume(state):
        return state

    if not state.get("n_replans") and not state.get("interrupt_handled"):
        _request_cost_approval(state, kind, targets, n_tasks, domain)
    return state


# ── Executor ────────────────────────────────────────────────
def executor_node(state: AgentState) -> AgentState:
    """plan 의 도구들을 순차 호출. 도구는 LLM 비호출."""
    from .. import tools as toolbox

    results: list[dict] = []
    evidence: list[dict] = []
    plan = state.get("plan") or []

    for step in plan:
        if turn_budget_exceeded(state):
            log.warning("[executor] turn budget exceeded — skip remaining")
            state["aborted_reason"] = "turn_budget"
            break
        tool_name = step.get("tool")
        args = step.get("args") or {}
        fn = getattr(toolbox, tool_name, None)
        if fn is None:
            log.warning(f"[executor] unknown tool: {tool_name}")
            continue
        try:
            out = fn(**args)
        except Exception as e:
            log.warning(f"[executor] {tool_name} failed: {e}")
            continue
        item = {"tool": tool_name, "purpose": step.get("purpose"), "args": args,
                "result": out}
        results.append(item)
        if tool_name == "search_documents":
            evidence.extend(out or [])

    # ── Fallback recovery (흡수: _legacy/v1 similar_hints 핵심) ────────────
    # 모든 도구가 빈 결과만 반환했고 검색이 안 돌았으면, 일반 retrieve 시도 → 사용자에게
    # "정보 부족" 만 보내는 대신 회복 경로 제공. 도메인 인식 (B20 fix):
    #   - auto / cross_domain → autograph.tools.search_documents_auto
    #     (manufacturer_id 메타로 finance 청크 제외 + nhtsa_* sources 한정)
    #   - finance (default) → autonexusgraph.tools.search_documents (corp_code 키)
    if state.get("aborted_reason") != "turn_budget":
        all_empty = all(not (r.get("result")) for r in results) if results else True
        searched_tools = {"search_documents", "search_documents_auto"}
        already_searched = any(r["tool"] in searched_tools for r in results)
        if all_empty and not already_searched and state.get("question"):
            domain = str(state.get("domain") or "finance").lower()
            q_text = state.get("question_rewritten") or state["question"]
            fb_tool: str | None = None
            fb_fn = None
            fb_args: dict = {}
            if domain in ("auto", "cross_domain"):
                try:
                    from autograph.tools import search_documents_auto as _auto_search
                    fb_tool = "search_documents_auto"
                    fb_fn = _auto_search
                    mids = state.get("target_makes") or []
                    fb_args = {
                        "query": q_text,
                        "top_k": 6,
                        # manufacturer_id 는 정수 list 필요 — target_models 의
                        # 모델 id 보다 manufacturer 단위가 더 fallback-friendly.
                        # state 에 manufacturer_id 가 따로 없으니 안전하게 미지정.
                    }
                    # 좁혀줄 수 있는 model_id 가 있으면 활용.
                    if state.get("target_models"):
                        fb_args["model_id"] = state["target_models"][0] if \
                            len(state["target_models"]) == 1 else state["target_models"]
                    log.info("[executor] all empty → fallback search_documents_auto "
                             "(domain=%s, models=%s)", domain,
                             state.get("target_models") or [])
                except Exception as e:   # noqa: BLE001
                    log.warning("[executor] auto fallback unavailable: %s", e)
                    fb_fn = None
            if fb_fn is None:   # finance 또는 auto import 실패 시 finance retrieve
                fb_tool = "search_documents"
                fb_fn = getattr(toolbox, "search_documents", None)
                targets = state.get("target_companies") or []
                fb_args = {
                    "query": q_text,
                    "top_k": 6,
                    "corp_code": targets[0] if len(targets) == 1 else (targets or None),
                }
                log.info("[executor] all empty → fallback search_documents (finance)")
            if fb_fn is not None:
                try:
                    fb_out = fb_fn(**fb_args)
                    if fb_out:
                        results.append({
                            "tool": fb_tool,
                            "purpose": "fallback_recovery",
                            "args": fb_args,
                            "result": fb_out,
                        })
                        evidence.extend(fb_out)
                        state["fallback_used"] = True
                except Exception as e:   # noqa: BLE001
                    log.warning("[executor] fallback %s failed: %s", fb_tool, e)

    state["tool_results"] = results
    state["evidence_chunks"] = evidence
    return state


# ── Synthesizer ─────────────────────────────────────────────
def synthesizer_node(state: AgentState,
                     *, llm_role: str = "synthesizer") -> AgentState:
    """tool_results + evidence_chunks → 자연어 답변 (LLM).

    cost guard: budget_aware_client + tracker 자동 통합.
    aborted_reason 이 있으면 fallback 답변 (LLM 비호출).
    """
    # 비용/예산 초과 → LLM 호출 안 하고 결정적 brief 로 fallback.
    abort = state.get("aborted_reason")
    if abort == "turn_budget":
        from .answering import build_deterministic_brief
        state["answer"] = (
            "이번 응답에서 사전 정의된 LLM 비용 한도를 초과했습니다.\n"
            "도구 결과 기반 결정적 brief 를 제공합니다 (LLM 합성 없음):\n\n"
            + build_deterministic_brief(state)
        )
        state["citations"] = []
        state["grounding"] = {"ok": False, "warnings": ["budget_exceeded"]}
        return state
    if abort == "cost_rejected":
        # 사용자가 비용 승인을 거절 — LLM 호출 없이 명시적 응답
        state["answer"] = (
            "사용자가 예상 비용을 승인하지 않아 답변을 생성하지 않았습니다. "
            "비용 한도를 조정하거나(.env: LLM_COST_AUTO_APPROVE_USD) 더 적은 컨텍스트로 다시 시도해주세요."
        )
        state["citations"] = []
        state["grounding"] = {"ok": False, "warnings": ["cost_rejected"]}
        return state

    # Pre-synth number guard (PRD §7.3) — 화이트리스트 + evidence 라벨링
    from .number_guard import (
        collect_approved_numbers,
        format_approved_for_prompt,
        sanitize_evidence_for_synth,
    )
    approved = collect_approved_numbers(state)
    sanitized_evidence = sanitize_evidence_for_synth(
        state.get("evidence_chunks") or [], approved,
    )

    # 도구 결과 + (정제된) evidence 를 요약해 LLM 입력으로
    context = _build_context(state, sanitized_evidence=sanitized_evidence)
    approved_line = format_approved_for_prompt(approved)
    messages = [
        {"role": "system", "content": (
            "당신은 한국 금융 분석가다. 사용자의 질문에 도구 출력과 본문 인용을 근거로 "
            "정확히 답변한다. 본문에 없는 내용은 추측하지 말 것.\n"
            "**중요 (재무 수치 가드):**\n"
            f"- 답변에 인용 가능한 정량 수치: {approved_line}\n"
            "- 그 외 숫자는 추정·합산·변환하지 말 것. 필요하면 '정보 부족' 으로 응답.\n"
            "- 본문 안 [검증불가:N] 표시 숫자는 답변에 절대 옮기지 말 것.\n"
            "- [수치:N] 표시는 검증된 수치 — 그대로 인용 가능.\n"
            "답변 끝에 [출처: corp_code, fiscal_year, section] 형식 인용을 붙인다."
        )},
        {"role": "user", "content": context},
    ]

    try:
        from ..llm.base import get_llm_client
        from ..llm.budget_aware import budget_aware_client
        from ..llm.cost_tracker import BudgetExceeded
        from ..config import turn_budget_for_domain

        domain = state.get("domain")
        hard_limit = turn_budget_for_domain(domain)
        client = budget_aware_client(
            get_llm_client(role=llm_role),
            caller=f"agent_synthesize:{str(domain or 'finance').lower()}",
            hard_limit=hard_limit,
        )
        resp = client.chat(messages, temperature=0.2, max_tokens=1200,
                           purpose="synthesize")
        state["answer"] = resp.content
        state["llm_usage_usd"] = float(state.get("llm_usage_usd") or 0.0) + resp.usage.cost_usd
    except BudgetExceeded:
        # 비용 한도 도달 — 결정적 brief 로 fallback (LLM 안 부름)
        from .answering import build_deterministic_brief
        state["answer"] = (
            "[LLM 비용 한도 도달 — 결정적 brief]\n\n"
            + build_deterministic_brief(state)
        )
        state["aborted_reason"] = "synth_budget"
    except Exception as e:
        log.warning(f"[synth] LLM failed: {e}")
        from .answering import build_deterministic_brief
        state["answer"] = (
            f"[LLM 합성 실패: {type(e).__name__} — 결정적 brief]\n\n"
            + build_deterministic_brief(state)
        )

    # citations 추출
    cits: list[dict] = []
    for ch in state.get("evidence_chunks") or []:
        cits.append({
            "chunk_id": ch.get("id"),
            "corp_code": ch.get("corp_code"),
            "fiscal_year": ch.get("fiscal_year"),
            "section": ch.get("section"),
            "rcept_no": ch.get("rcept_no"),
            "score": ch.get("score"),
        })
    state["citations"] = cits[:10]

    # 답변 grounding 검증 — LLM 답변이 evidence 와 일치하는지
    from .grounding import verify_answer_grounding
    grounding = verify_answer_grounding(
        answer=state.get("answer", ""),
        evidence_chunks=state.get("evidence_chunks") or [],
    )
    state["grounding"] = grounding
    if not grounding["ok"]:
        log.warning(f"[synth] grounding failed: {grounding['warnings']}")
    return state


def _build_context(state: AgentState, *,
                    sanitized_evidence: list[dict] | None = None) -> str:
    """tool_results + evidence_chunks → LLM 입력 텍스트.

    sanitized_evidence 가 주어지면 그것을 사용 (number_guard 가 라벨링한 본문).
    None 이면 원본 evidence_chunks 그대로 (이전 호환).
    """
    parts: list[str] = []
    parts.append(f"[질문]\n{state.get('question','')}\n")

    parts.append("[질문 유형] " + (state.get('question_kind') or 'unknown') + "\n")

    tools_out = state.get("tool_results") or []
    if tools_out:
        parts.append("[도구 결과]")
        for t in tools_out:
            preview = str(t.get("result"))[:1000]
            parts.append(f"- {t['tool']} ({t.get('purpose','')}): {preview}")
        parts.append("")

    ev = sanitized_evidence if sanitized_evidence is not None else (state.get("evidence_chunks") or [])
    if ev:
        parts.append("[본문 인용]")
        for c in ev[:6]:
            score = c.get('score')
            score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "?"
            parts.append(
                f"- corp={c.get('corp_code')} year={c.get('fiscal_year')} "
                f"sec={(c.get('section') or '')[:30]} score={score_s}\n"
                f"  {(c.get('text') or '')[:400]}"
            )
        parts.append("")

    parts.append("위 근거만 사용해 한국어로 답변하고, 끝에 [출처:...] 인용을 남기세요. "
                  "근거 부족 시 '정보 부족' 으로 답하세요.")
    return "\n".join(parts)


__all__ = ["triage_node", "planner_node", "executor_node", "synthesizer_node"]
