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

    # 회사 식별 — 질문에서 회사명 후보 추출 + lookup_company
    targets: list[str] = []
    # 간단한 후보 추출: 명사 형태소가 없으므로 흔한 회사명 패턴 시도. 후속 LLM 보강 여지.
    for word in q.split():
        if len(word) >= 2:
            try:
                hits = lookup_pg(word, limit=1)
            except Exception:
                hits = []
            for h in hits:
                if h.get("corp_code") and h["corp_code"] not in targets:
                    targets.append(h["corp_code"])
                    break
        if len(targets) >= 5:
            break
    # 세션 entity carry-over — 이번 turn 에 회사가 식별 안 되고 multi-turn 이면
    # 이전 세션의 target_companies/persons 를 borrow (PRD §7.6.2).
    thread_id = state.get("thread_id") or ""
    prev = session.get(thread_id) if thread_id else None
    if not targets and prev and prev.target_companies:
        targets = list(prev.target_companies)
        state["session_carryover"] = True
        log.info("[triage] carry-over targets from session: %s", targets)

    state["target_companies"] = targets

    # 이번 turn 결과를 세션에 기록 (다음 turn 의 carry-over 재료)
    if thread_id:
        year_hint = extract_year_hint(state.get("question_rewritten") or q)
        session.update(
            thread_id,
            target_companies=targets if targets else None,
            last_year=year_hint,
            last_question_kind=kind,
            last_question=q,
        )

    log.info(f"[triage] kind={kind} targets={targets}")
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
    # 모든 도구가 빈 결과만 반환했고 search_documents 도 안 돌았으면, 일반 retrieve
    # 시도 → 사용자에게 "정보 부족" 만 보내는 대신 회복 경로 제공.
    if state.get("aborted_reason") != "turn_budget":
        all_empty = all(not (r.get("result")) for r in results) if results else True
        already_searched = any(r["tool"] == "search_documents" for r in results)
        if all_empty and not already_searched and state.get("question"):
            log.info("[executor] all empty → fallback search_documents")
            try:
                fn = getattr(toolbox, "search_documents", None)
                if fn is not None:
                    targets = state.get("target_companies") or []
                    fb_args = {
                        "query": state.get("question_rewritten") or state["question"],
                        "top_k": 6,
                        "corp_code": targets[0] if len(targets) == 1 else (targets or None),
                    }
                    fb_out = fn(**fb_args)
                    if fb_out:
                        results.append({
                            "tool": "search_documents",
                            "purpose": "fallback_recovery",
                            "args": fb_args,
                            "result": fb_out,
                        })
                        evidence.extend(fb_out)
                        state["fallback_used"] = True
            except Exception as e:
                log.warning(f"[executor] fallback search failed: {e}")

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
    if state.get("aborted_reason") == "turn_budget":
        from .answering import build_deterministic_brief
        state["answer"] = (
            "이번 응답에서 사전 정의된 LLM 비용 한도를 초과했습니다.\n"
            "도구 결과 기반 결정적 brief 를 제공합니다 (LLM 합성 없음):\n\n"
            + build_deterministic_brief(state)
        )
        state["citations"] = []
        state["grounding"] = {"ok": False, "warnings": ["budget_exceeded"]}
        return state

    # 도구 결과 + evidence 를 요약해 LLM 입력으로
    context = _build_context(state)
    messages = [
        {"role": "system", "content": (
            "당신은 한국 금융 분석가다. 사용자의 질문에 도구 출력과 본문 인용을 근거로 "
            "정확히 답변한다. 본문에 없는 내용은 추측하지 말 것. "
            "수치는 도구 결과(get_revenue / get_operating_income 등) 만 인용하고, "
            "답변 끝에 [출처: corp_code, fiscal_year, section] 형식 인용을 붙인다."
        )},
        {"role": "user", "content": context},
    ]

    try:
        from ..llm.base import get_llm_client
        from ..llm.budget_aware import budget_aware_client
        from ..llm.cost_tracker import BudgetExceeded
        from ..config import get_settings

        settings = get_settings()
        client = budget_aware_client(
            get_llm_client(role=llm_role),
            caller="agent_synthesize",
            hard_limit=settings.agent_turn_budget_usd,
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


def _build_context(state: AgentState) -> str:
    """tool_results + evidence_chunks → LLM 입력 텍스트."""
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

    ev = state.get("evidence_chunks") or []
    if ev:
        parts.append("[본문 인용]")
        for c in ev[:6]:
            parts.append(
                f"- corp={c.get('corp_code')} year={c.get('fiscal_year')} "
                f"sec={c.get('section','')[:30]} score={c.get('score'):.3f}\n"
                f"  {c.get('text','')[:400]}"
            )
        parts.append("")

    parts.append("위 근거만 사용해 한국어로 답변하고, 끝에 [출처:...] 인용을 남기세요. "
                  "근거 부족 시 '정보 부족' 으로 답하세요.")
    return "\n".join(parts)


__all__ = ["triage_node", "planner_node", "executor_node", "synthesizer_node"]
