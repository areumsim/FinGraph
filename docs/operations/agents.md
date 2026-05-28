# 에이전트 운영 가이드

본 문서는 FinGraph 의 LangGraph 기반 에이전트 계층 (PRD §7.5 / §7.6) 의 구조·진입점·
운영 절차를 정리한다. 데이터 적재는 [`data_pipeline.md`](./data_pipeline.md), 도구 API
스펙은 [`rag_tools.md`](./rag_tools.md) 참조.

## 1. 계층 구조 (단방향 의존)

```
[UI / API 진입점]
  Streamlit ui/app.py      — chat_message · st.status 노드 진행 · 👍/👎/📝
  FastAPI api/main.py      — /chat (blocking) · /chat/stream (SSE)
        │
        ▼
[에이전트 (LangGraph StateGraph)]
  agents/graph.py          — StateGraph 빌드 + run_agent / run_agent_stream
  agents/state.py          — AgentState TypedDict (대화 1 turn 의 누적 상태)
  agents/nodes.py          — triage / planner / synthesizer (+legacy executor)
  agents/supervisor.py     — DAG 의존성 라우팅 + Send API 병렬 디스패치 (PRD §7.5.7)
  agents/workers.py        — research / graph / sql / calculator 4 worker (PRD §7.5.2)
  agents/dag.py            — make_task / unblocked / topologically_valid 헬퍼
  agents/validator.py      — validation + replan (n<=2, tasks/result 자동 리셋)
  agents/policy.py         — 룰 기반 question_kind 분류, budget 가드
  agents/answering.py      — LLM 비호출 결정적 brief (폴백)
  agents/grounding.py      — 답변 ↔ evidence overlap, citation 검증
  agents/temporal.py       — "작년"/"최근 N년" → 절대 연도
  agents/rewriter.py       — 멀티턴 coreference 해소 (LLM)
  agents/session.py        — thread 별 entity TTL 메모리 (in-memory, LRU)
  agents/checkpointer.py   — PG (chat schema) / Memory 자동 선택
  agents/tracing.py        — Langfuse / LangSmith fail-soft
  agents/interrupts.py     — HITL payload + clarification/cost_approval (PRD §7.5.6)
  agents/cost_estimator.py — Planner 산출 비용 추정 (cost_approval 가드)
  agents/number_guard.py   — Pre-synth 화이트리스트 + evidence 라벨링 (PRD §7.3)
        │
        ▼
[안전 가드]
  safety/prompt_safety.py  — XML escape + injection 시그널 감지
  safety/cypher_guard.py   — Cypher 정적 READ-ONLY 검사 (tools/graph 가 호출)
  safety/language_guard.py — 답변 한국어 비율 검사
        │
        ▼
[사전 정의 도구 풀 — 자유 SQL/Cypher 금지 (PRD §7.5.9/10)]
  tools/financials.py            — finance PG: lookup_company, get_revenue, …
  tools/graph.py                 — finance Neo4j: list_subsidiaries, find_paths, …
  tools/retrieve.py              — pgvector + meta filter: search_documents
  autograph/tools/spec.py        — auto PG: lookup_vehicle, get_spec, compare_vehicles, …
  autograph/tools/graph.py       — auto Neo4j: list_recalls_affecting, list_components, …
  autograph/tools/retrieve.py    — pgvector + mfr/model/variant meta: search_documents_auto
  autograph/tools/bridge.py      — cross-domain: bridge_corp_to_entity, …
        │
        ▼
[저장소]
  Neo4j 5.18 + PostgreSQL 16 (pgvector) + (옵션) Qdrant
```

## 1.5. 도메인 라우팅 (`finance` / `auto` / `cross_domain`)

PRD v2.1 부터 단일 에이전트가 **금융 (FinGraph) + 자동차 (AutoGraph) + 둘의 교차** 를 모두 처리.
도메인 분기는 4 지점에 명시적 — LLM 자유 판단 영역 없음.

```
[1] _init_state (agents/graph.py:370)
    ├─ run_agent(question, domain=None|"finance"|"auto"|"cross_domain")
    ├─ domain 미지정 → autograph.policy.route_domain(question) 호출
    │     · KW_FIN ∩ KW_AUTO 동시 등장   → "cross_domain"
    │     · KW_AUTO_GENERIC|RECALL|SPEC  → "auto"
    │     · 그 외                         → "finance" (기본)
    └─ state["domain"] 에 기록 — 이후 모든 노드가 참조
        │
        ▼
[2] planner_node (agents/nodes.py)
    ├─ state["domain"] == "auto"
    │     → autograph.policy.plan_auto_tasks(question, target_vehicles, target_models)
    │     · classify_question_auto() → vehicle_spec / recall / complaint / supply_chain /
    │       compare / narrative
    │     · question kind 별로 SQL / graph / research task DAG 생성
    │     · 모든 task 에 lookup_vehicle 1건 선행 (식별)
    ├─ state["domain"] == "cross_domain"
    │     → autograph.policy.plan_cross_domain_tasks(question, target_companies)
    │     · auto research/graph + bridge_corp_to_entity + finance SQL 혼합 DAG
    └─ state["domain"] == "finance" (기본)
          → fingraph.agents.policy.plan_tasks() — 기존 finance planner
        │
        ▼
[3] workers._allowed_intents (agents/workers.py)
    ├─ agent 별 (sql / graph / research / calculator) 화이트리스트:
    │     · _FIN_GRAPH_ALLOWED = {list_subsidiaries, get_executives, ...}
    │     · _AUTO_GRAPH_ALLOWED = {list_recalls_affecting, list_components, ...}
    │     · _FIN_SQL_ALLOWED   = {get_revenue, get_operating_income, ...}
    │     · _AUTO_SQL_ALLOWED  = {lookup_vehicle, get_spec, compare_vehicles, ...}
    └─ domain 별 조합:
          finance      → _FIN_*
          auto         → _AUTO_*
          cross_domain → _FIN_* ∪ _AUTO_*
        │
        ▼
[4] workers._toolbox_for (agents/workers.py)
    └─ intent 호출 시 import 대상 결정:
          intent ∈ _AUTO_*  → from autograph.tools import {intent}
          intent ∈ _FIN_*   → from fingraph.tools  import {intent}
```

**Cypher 템플릿 통합**: `autograph.cypher_templates_auto.AUTO_TEMPLATES`
(`auto_*` 접두사 22개) 는 `autograph.tools` import 시 **1회 side-effect** 로
`fingraph.tools.cypher_templates.TEMPLATES` 에 병합. 따라서 worker 는
`render_template("auto_recalls_by_variant", ...)` 와 `render_template("list_subsidiaries", ...)`
를 동일 함수로 호출 — 같은 cypher_guard / param schema 검증 통과.

**회귀 안전성**: domain 미지정 finance gold (기존 `eval/qa_gold/gold_qa_v0.jsonl`) 는
`route_domain` 이 키워드 부재로 `"finance"` 반환 → 기존 finance planner 동일 경로.
`tests/test_autograph_routing.py::test_planner_node_finance_unaffected_when_no_domain` 가 보장.

**API / UI 노출**: `POST /chat` 의 `ChatRequest.domain` (optional), Streamlit
사이드바 라디오 (auto-detect / finance / auto / cross_domain). 미지정 = 자동 판정.

### 빠른 진단

```python
from autograph.policy import route_domain, classify_question_auto
from fingraph.agents.graph import _init_state

route_domain("Hyundai Sonata 2024 리콜")           # → 'auto'
route_domain("현대자동차 2024 매출과 그랜저 리콜")  # → 'cross_domain'
route_domain("삼성전자 2024년 매출")                # → 'finance'
classify_question_auto("Tesla Model Y 2023 리콜")   # → 'vehicle_recall'

s = _init_state("Tesla recall", "tid", None)
s["domain"]   # → 'auto'
```


## 2. 한 turn 의 실행 흐름 (PRD §7.5.2 / §7.5.3 / §7.5.7)

```
User Query
   ↓
[Triage] (agents/nodes.py)
   ├─ prompt_safety.sanitize_user_input — XML escape + injection 시그널
   ├─ rewriter.rewrite_query — "그 중" / "위 회사들" 해소 (history 있을 때만)
   ├─ temporal.normalize_temporal_terms — "작년" → "2025년"
   ├─ policy.classify_question — factual / structural / narrative / multi_hop
   ├─ tools.financials.lookup_company — 회사명 → corp_code
   └─ session.update — 다음 turn carry-over 용 entity 저장
   ↓
[Planner] (agents/nodes.py)
   question_kind 별 룰 → state["tasks"]: DAG (PRD §7.5.3)
     · factual    → SQL × N task (회사당 get_revenue + get_operating_income)
     · structural → Graph × 3 task (subsidiaries / executives / shareholders)
     · narrative  → Research × 1 task (search_documents)
     · multi_hop  → Graph(병렬) → SQL(graph 의존) + Research(병렬)
   호환: state["plan"] (flat) 도 함께 채움 → tasks 비었을 때 executor 폴백
   ↓
[Supervisor] (agents/supervisor.py)
   ├─ dag.topologically_valid — 순환 의존성 검출
   ├─ dag.unblocked_tasks — depends_on 모두 done 인 pending tasks
   ├─ langgraph 활성: Send API 로 worker 노드 N개 동시 디스패치 (병렬)
   └─ 함수 체인: sequential dispatch
   ↓                                ↑ 모든 task 완료 시 synthesizer
[Workers] (agents/workers.py)        │
   research_worker  ───┐             │
   graph_worker     ───┤  병렬 실행 ┘
   sql_worker       ───┤  (의존성 없는 task)
   calculator_worker───┘
     각 worker:
     · 자기 도메인 도구만 호출 (sql 은 sql tools, graph 는 graph tools …)
     · task.status / task.result 갱신 → state.task_results
     · 실패 시 task.status="failed" — supervisor 가 다른 task 계속
   ↓
[Synthesizer] (agents/nodes.py)
   ├─ budget_aware_client + cost_tracker (circuit breaker)
   ├─ tool_results + evidence_chunks → LLM chat
   └─ grounding.verify_answer_grounding — overlap / citation 검증
   ↓
[Validator] (agents/validator.py)
   ├─ 길이 / 한국어 비율 / grounding / 재무 수치 환각 검사
   ├─ self-reported "정보 부족" 은 통과
   └─ failed + n_replans<2 → Planner 로 replan
        (mark_replan: tasks / task_results / tool_results / answer 모두 리셋 + 카운터++)
   ↓
[Finalize]
   replan 한도 도달 시 ⚠️ prefix + validation_issues 노출
   ↓
PG 적재 (chat.messages + chat.checkpoints) + UI 렌더 (citations, agent_trace, grounding, feedback)
```

## 3. AgentState (`agents/state.py`)

| 필드 | 출처 | 용도 |
|---|---|---|
| `thread_id`, `question`, `history` | 진입점 | 입력 |
| `question_rewritten` | rewriter / temporal | 정규화 후 query |
| `temporal_audit` / `rewrite_audit` | rewriter / temporal | 정규화 trail |
| `safety_signals` | prompt_safety | injection 시그널 (telemetry) |
| `question_kind` | policy | factual / structural / narrative / multi_hop |
| `target_companies` | triage | corp_code 목록 |
| `session_carryover` | triage | 이전 turn entity borrow 여부 |
| `tasks` | **planner** | **DAG (PRD §7.5.3)** — `[{id, agent, intent, args, depends_on, status, result}]` |
| `task_results` | supervisor / workers | `task_id → result` append-only |
| `plan` | planner | legacy flat — tasks 비었을 때 executor 폴백 |
| `tool_results`, `evidence_chunks` | workers / executor | 도구 출력 누적 |
| `graph_subgraph` | graph_worker (`get_subgraph`) | 시각화용 |
| `fallback_used` | executor | 빈 결과 회복 trigger |
| `aborted_reason` | 어디서나 | `turn_budget` / `synth_budget` / `exception` |
| `answer`, `citations` | synthesizer | LLM 결과 |
| `grounding` | synthesizer/validator | overlap·citation 검증 결과 |
| `validation_status`, `validation_issues` | validator | `passed` / `failed` + 사유 |
| `n_replans`, `llm_usage_usd` | graph | replan 카운터, 누적 비용 |

`tasks` 각 항목 스키마 (PRD §7.5.3):
- `id`: 고유 식별자 (planner 가 `g_1`, `sql_2` 등 prefix+seq 로 부여)
- `agent`: `"research" | "graph" | "sql" | "calculator"`
- `intent`: worker 안에서 라우팅 키 (graph 의 `list_subsidiaries` / sql 의 `get_revenue` 등)
- `args`: 도구 호출 파라미터 dict
- `depends_on`: 이 task 가 시작 전 완료돼야 할 다른 task id 목록
- `status`: `"pending" | "running" | "done" | "failed" | "skipped"`
- `result`: worker 가 채우는 도구 출력 (또는 `{"error": ...}`)

## 4. 노드 진입점

| 함수 | 위치 | 호출자 |
|---|---|---|
| `run_agent(question, thread_id, history)` | `agents/graph.py` | `api/main.py:/chat`, 기타 동기 호출 |
| `run_agent_stream(...)` → `Iterator[(node, partial_state)]` | `agents/graph.py` | `api/main.py:/chat/stream`, `ui/app.py` |
| `_run_with_langgraph` ↔ `_run_with_fallback_chain` | `agents/graph.py` | `_HAS_LANGGRAPH` 분기 자동 |

`_HAS_LANGGRAPH` 가 False (langgraph 미설치) 면 Python 함수 체인이 동일 흐름·동일 state
포맷으로 동작. 테스트 환경에서 의존성 없이 검증 가능.

## 5. Replan 루프 (PRD §7.5.5)

```
synthesizer ─→ validator ─┬─ passed → finalize → END
                         └─ failed + n_replans<MAX_REPLANS(2)
                                ↓ mark_replan (state 리셋 + n_replans++)
                                ↓
                            planner ↑ (반복)
```

MAX_REPLANS 도달 시 `answer = "⚠️ 검증 실패 (replan 2/2): ..." + 마지막 답변`.
사용자는 신뢰도 판단 가능.

## 6. 체크포인트 (PRD §7.5.8)

`agents/checkpointer.py` 의 우선순위:
1. `LANGGRAPH_CHECKPOINT_DSN` env (전용 PG 풀)
2. `FINGRAPH_PG_DSN` env
3. `POSTGRES_DSN` env
4. `config.postgres_dsn`

성공 시 `chat` 스키마에 4개 테이블 (`checkpoints`, `checkpoint_writes`,
`checkpoint_blobs`, `checkpoint_migrations`) 자동 생성. search_path 주입 방식으로
스키마 격리. backend env (`LANGGRAPH_CHECKPOINT_BACKEND`):

- `auto` (기본) — PG 시도 → memory 폴백
- `memory` / `in_memory` — 강제 in-memory
- `none` — checkpoint 비활성

## 7. Tracing (PRD §7.5.11)

`.env` 의 `TRACE_BACKEND`:

| 값 | 동작 |
|---|---|
| `langfuse` | `LANGFUSE_HOST`/`PUBLIC_KEY`/`SECRET_KEY` 필요. CallbackHandler 가 노드별 span 전송 |
| `langsmith` | `LANGSMITH_API_KEY`/`LANGSMITH_PROJECT` 필요. langgraph 가 환경변수로 자동 전송 |
| 빈 값 / `none` | tracing OFF |

`make trace-on` 으로 현재 활성 상태 확인. SDK 미설치 / 키 누락은 silent skip.

## 7.5. Human-in-the-Loop (PRD §7.5.6)

`agents/interrupts.py` 의 `request_interrupt(payload)` 가 LangGraph `interrupt()` 호출.
graph 가 멈추고 client (UI/SSE) 가 응답을 주면 같은 thread 의 checkpoint 부터 재개.

발동 시점 (이번 PR — Clarification 만; cost approval / sensitive 는 동일 helper 로 후속):

| 시점 | 조건 | 페이로드 kind | 응답 형식 |
|---|---|---|---|
| Triage 회사 식별 | `is_ambiguous_company(hits)` (후보 ≥ 2 + margin < 10%) | `company_clarification` | `{"index": N}` / `{"corp_code": "0012…"}` / 8자리 str / int |
| Planner 산출 후 | `estimate_turn_cost > LLM_COST_AUTO_APPROVE_USD` (기본 $0.50) | `cost_approval` | `True/False` / `"yes"·"no"·"승인"·"거절"` / `{"approved": bool}` |

폴백 (langgraph 미설치) — `request_interrupt` 가 `InterruptUnavailable` raise →
- clarification: triage 가 1순위 후보 자동 선택 + `safety_signals: ambiguous_company_auto_resolved:삼성->00126380`
- cost_approval: planner 가 자동 통과 + `safety_signals: cost_approval_auto_passed:$0.7521`

비용 추정 (`agents/cost_estimator.py`): Synthesizer LLM 호출 비용 = 시스템 프롬프트(200 tok) +
질문 + 도구 결과(최대 1000자 × N) + evidence(최대 6×400자) + 출력(1200 tok 기본).
모델 단가는 `llm/cost.py:PRICING`. `replan_factor = MAX_REPLANS + 1` 을 곱해 over-estimate.

거절 처리:
- `state.aborted_reason = "cost_rejected"`
- Supervisor 가 pending tasks 모두 `skipped` 로 마킹 (worker 호출 안 함)
- Synthesizer 가 LLM 없이 "사용자가 예상 비용을 승인하지 않아…" 명시적 답변

진입점 / API:
- `run_agent_stream` 이 interrupt 발생 시 `("__interrupt__", state)` yield 후 종료
- FastAPI `/chat/stream` 이 `data: {"node":"__interrupt__","pending_interrupt":{...}}` SSE 이벤트
- 클라이언트가 `POST /chat/resume {thread_id, response}` 호출 → `run_agent_resume_stream`
  이 `Command(resume=response)` 로 재개
- Streamlit `render_clarification(payload, key_prefix)` 가 후보 라디오 + 선택 시 resume 호출

```
User              UI               API               LangGraph
 │  "삼성 매출은?"  │                 │                  │
 │ ───────────────▶│  POST /chat/stream                  │
 │                 │ ───────────────▶│  invoke(state)    │
 │                 │                 │ ────────────────▶│  triage: ambiguous
 │                 │                 │                  │  interrupt(payload)
 │                 │                 │  SSE __interrupt__│ ◀──────────────
 │                 │ ◀───────────────│                  │  [paused checkpoint]
 │                 │  candidates 라디오                  │
 │  선택           │                 │                  │
 │ ───────────────▶│  POST /chat/resume {index: 1}      │
 │                 │ ───────────────▶│  invoke(Command(resume=...))      │
 │                 │                 │ ────────────────▶│  triage 재개
 │                 │                 │                  │  ... → __final__
 │                 │  SSE __final__  │ ◀────────────────│
 │  답변 표시      │ ◀───────────────│                  │
```

## 7.7. Pre-synth Number Guard (PRD §7.3)

"재무 수치는 절대 LLM 이 생성하지 않는다" 원칙을 입력 단계에서 강제. Validator 는
post-hoc 검사라 사용자에게 잘못된 숫자가 노출될 수 있다. number_guard 는 그 전에
synthesizer 입력을 정제.

```
collect_approved_numbers(state)
  → tool_results + evidence_chunks 의 큰 숫자(콤마≥2 또는 7자리 이상) 수집

sanitize_evidence_for_synth(evidence, approved)
  → evidence 본문에서 큰 숫자를 두 라벨로 마킹:
     [수치:N]      — approved 화이트리스트에 있음 (LLM 인용 가능)
     [검증불가:N]  — approved 에 없음 (LLM 이 인용 금지)

format_approved_for_prompt(approved)
  → system prompt 의 "인용 가능 수치" 절에 들어갈 한 줄 (상위 10개 + 외 N개)
```

Synthesizer system prompt 가 명시:
- 답변 가능 수치는 화이트리스트로 한정
- 본문의 `[검증불가:N]` 은 답변에 옮기지 말 것
- `[수치:N]` 만 그대로 인용

corp_code (leading-0 8자리) / 4자리 연도 / 소수점 비율 (9.5%) 은 `_BIG_NUMBER_RE` 가
원천 제외. validator 와 동일한 정의를 공유 → 입출력 가드 일관성.

## 8. Streaming (PRD §7.6.5)

`run_agent_stream` 이 `(node_name, partial_state)` yield. 마지막은 항상 `('__final__', ...)`.

- FastAPI `/chat/stream` 이 이를 SSE 로 직렬화 (`data: {...}\n\n`), 마지막에 `data: [DONE]`
- Streamlit `ui/app.py` 가 `st.status` + `render_progress_chip` 으로 chip 갱신
- 노드 라벨: `🔍 Triage / 🧭 Planner / 🛠️ Executor / ✍️ Synthesizer / ✅ Validator / ♻️ Replan / 🏁 Finalize`

폴백 체인 (langgraph 미설치) 도 동일 시퀀스로 yield 하므로 UI 코드는 분기 불필요.

## 9. 비용 가드 (사용자 명시 — 늘 적용)

| 가드 | 위치 | 동작 |
|---|---|---|
| 누적 hard limit | `llm/cost_tracker.py` | `LLM_COST_HARD_LIMIT_USD` 도달 시 `BudgetExceeded` 즉시 abort |
| 자동 승인 한도 | `llm/cost_tracker.py` | `LLM_COST_AUTO_APPROVE_USD` 이하면 자동 진행. 초과 시 `--approve-cost` 필요 |
| Turn budget | `agents/policy.py:turn_budget_exceeded` | `AGENT_TURN_BUDGET_USD` (기본 $0.20) 초과 시 executor·synthesizer 즉시 fallback |
| Circuit breaker | `extractors/engine.py`, `llm/budget_aware.py` | 누적 실패 N회 → 단기 차단 |
| Cost 누적 표시 | `ui/components.py:render_cost_badge` | 세션 / turn 비용 사이드바 노출 |

## 10. 세션 entity carry-over (`agents/session.py`)

```
turn N:
  triage 가 회사 식별 실패 + thread_id 의 prev_session 존재
    → state["target_companies"] = prev_session.target_companies
    → state["session_carryover"] = True
  답변 후 session.update(thread_id, target_companies=..., last_year=...)
turn N+1:
  rewriter 가 LLM 으로 "그 중" 풀어줌 + session 이 entity 백업.
  rewriter LLM 실패해도 session 으로 fallback.
```

TTL 3600s, LRU 256 (env `FINGRAPH_SESSION_TTL` / `_MAX` 로 조정).

## 11. 테스트 매트릭스

| 모듈 | 테스트 | 케이스 |
|---|---|---|
| temporal | `tests/test_temporal.py` | 단일·범위·미정규화·explicit·range bound |
| safety | `tests/test_safety.py` | escape / injection / cypher guard 4종 / korean ratio |
| rewriter | `tests/test_rewriter.py` | history 게이트, env disable, short follow-up |
| validator | `tests/test_validator.py` | 환각 숫자 / 한국어 / replan max / mark_replan 리셋 |
| session | `tests/test_session.py` | TTL / LRU / snapshot 격리 / clear |
| executor fallback | `tests/test_executor_fallback.py` | 빈 결과 회복 / 이미 search / 예산 초과 시 skip |
| graph | `tests/test_graph_smoke.py` | clean / replan / max 도달 ⚠️ |
| checkpointer | `tests/test_checkpointer.py` | DSN 우선순위 / search_path 인코딩 / redact |
| tracing | `tests/test_tracing.py` | backend 결정 / fail-soft / 캐시 무효화 |
| stream | `tests/test_stream.py` | 노드 시퀀스 / replan event / partial state 누적 |
| **dag** | `tests/test_dag.py` | make_task / unblocked / 의존성 / 순환 검출 / filter_by_agent |
| **workers** | `tests/test_workers.py` | 4 worker × intent 허용·차단·실패·dispatch / Calculator 인젝션 차단 |
| **supervisor** | `tests/test_supervisor.py` | sequential dispatch / 의존성 순서 / 순환 skip / 예산 차단 / Send 디렉티브 |
| **planner DAG** | `tests/test_planner_dag.py` | question_kind 별 DAG 구성 / multi_hop 의존성 / year_hint / 빈 plan |
| **interrupts** | `tests/test_interrupts.py` + `test_triage_interrupt.py` | payload / ambiguity / coerce / 폴백 자동 해결 / resume |
| **cost approval** | `tests/test_cost_estimator.py` + `test_cost_approval.py` | 비용 추정 단조성 / 임계 / resume / 거절 / 폴백 |
| **cypher templates** | `tests/test_cypher_templates.py` | 레지스트리 무결성 / param schema (type/range/regex) / hops·depth 변형 / bool reject |
| **number guard** | `tests/test_number_guard.py` | 화이트리스트 수집 / 라벨링 / 원본 불변 / cap·text_max / prompt 형식 |

`make test` (integration 제외) — **245 passed**.

## 12. 운영 체크리스트

```bash
# 1) langgraph 활성 확인
make enable-langgraph
#   ✓ langgraph import 성공
#   ✓ _HAS_LANGGRAPH = True
#   ✓ checkpointer = PostgresSaver

# 2) checkpoint 테이블 점검
psql $POSTGRES_DSN -c "\dt chat.checkpoint*"
#   chat.checkpoints / .checkpoint_writes / .checkpoint_blobs / .checkpoint_migrations

# 3) tracing 활성 확인 (옵션)
make trace-on
#   tracing: langsmith project=fingraph key=set
#     또는 tracing: langfuse host=... keys=set

# 4) 스트리밍 end-to-end (SSE)
make serve-api &
curl -N -H "Accept: text/event-stream" \
     -X POST http://localhost:31020/chat/stream \
     -d '{"thread_id":"smoke","message":"삼성전자 작년 매출은?"}'
#   → triage / planner / executor / synthesizer / validator / __final__ event

# 5) UI 가동
make serve-ui
#   → http://localhost:31021 — 노드별 chip + 출처 + 👍/👎/📝
```

## 13. 확장 포인트 (다음 PR 후보)

PRD §7.5 9 agents 중 현재 **Triage / Planner / Supervisor / Research / Graph / SQL / Calculator / Synthesizer / Validator = 9 / 9 구현**.
Calculator 의 Python sandbox 격리는 인프라 후속.

다음 단계 권장 순서:

1. ~~Supervisor + Worker 4종 + Send API 병렬~~ — **✅ 완료** (PRD §7.5.2 / §7.5.7)
2. ~~Human-in-the-Loop interrupt (Clarification + Cost approval)~~ — **✅ 완료** (PRD §7.5.6). sensitive_decision 은 동일 helper 로 후속
3. ~~Cypher 템플릿 레지스트리~~ — **✅ 완료** (PRD §7.5.9). 22 template, param schema 검증
4. ~~Pre-synth number guard~~ — **✅ 완료** (PRD §7.3). 화이트리스트 + 라벨링
5. **Calculator Python sandbox** — e2b / daytona / 자체 docker 격리 (PRD §7.5.11) — 다음 권장
6. **Planner LLM 업그레이드** — 현재 룰 기반 DAG → LLM 이 JSON Schema 로 DAG 생성 (PRD §7.5.12)
7. **API rate limit** (slowapi) + **audit logging default-on** + **per-agent system prompts 버전 관리**
8. **`/health` 보강** — embedding · reranker · LLM provider ping 추가
9. **sensitive_decision interrupt** — 외부 보고용 / 민감 답변 시 동의 확인

각 항목은 별도 PR. 상세는 README §7 로드맵 + PRD §7.5 참조.
