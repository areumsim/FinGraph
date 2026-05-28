# AutoNexusGraph

> **자동차 제품·부품·리콜·공급망 (auto) + 한국 상장사 공시·재무 (finance) 두 도메인을 그래프·정형·벡터 하이브리드로 추론하고, `bridge.corp_entity` 로 Cross-Domain 까지 한 turn 안에 묶는 멀티도메인 GraphRAG 에이전트.**

자동차 OEM/부품사 ↔ 재무 데이터를 한 질문으로 추적 (예: "현대모비스 매출과 모비스가 공급하는 차종의 최근 리콜은?"). Vector 단독 RAG 가 풀지 못하는 멀티홉 / Cross-Domain 추론을 Graph(Neo4j) + SQL(PostgreSQL) + Vector(pgvector) 하이브리드로 해결. Azure 종속 제거, LLM Provider(OpenAI / Anthropic / 로컬) 환경변수 교체 가능. 도메인 모드는 사용자 hint 또는 키워드 자동 라우팅 — `auto` / `finance` / `cross_domain`.

상세 요구사항은 [PRD.md](./PRD.md) (v2.1) · AutoGraph(자동차) 전용 가이드는 [docs/autograph.md](./docs/autograph.md) 참조.

> **구성 요약:**
> - **Core** (`src/autonexusgraph/`) — LangGraph multi-agent, LLM 어댑터, safety guard, DB/embedding/평가 harness 공유 인프라. Phase 4.7 완료 (Send 병렬 / Validator·Replan / HITL clarification·cost approval / Cypher 템플릿 레지스트리 / Pre-synth number guard / PG checkpoint / streaming / tracing).
> - **Finance 도메인** (`src/autonexusgraph/tools/financials,graph,retrieve`) — DART 공시 / KRX 마스터 / ECOS / Wikidata / Wikipedia / SEC EDGAR / GLEIF / 연합뉴스 RSS / KCGS ESG → 코스피200+코스닥100.
> - **Auto 도메인** (`src/autograph/`) — NHTSA(vPIC/Recalls/Complaints) + Wikidata(manufacturers/models/suppliers) + AI Hub(부품 결함 / 자율주행 진단) + KOTSA 수리검사 등.
> - **Cross-Domain Bridge** — `bridge.corp_entity` 가 두 도메인을 wikidata_qid / LEI / 사업자번호 / 이름으로 매칭. 현대자동차·현대모비스·현대위아·한국타이어 등 4 개 한국 OEM/부품사가 corp_code 와 직접 연결됨.

---

## 1. 한눈에 보는 현황

### Finance 도메인 (코스피200 + 코스닥100)

| 영역 | 적재량 | 비고 |
|---|---:|---|
| `master.companies` (코스피200+코스닥100) | 295 | 활성 회사 |
| `master.entity_map` (ticker/QID/LEI/CIK/ISIN/…) | 1,979 | 10 종 외부 ID |
| `master.persons` / 임원 이력 | 9,948 / 22,303 | (name, birth_year) 분리 |
| `fin.financials` (XBRL) / `fin.filings` | 184K / 4.6K | 3년치 |
| `news.articles` / 멘션 | 338 / 141 | 연합뉴스 RSS 3종 |
| `wiki.wikipedia_pages` / `wiki.wikidata_facts` | 276 / 466 | 93.6% / 55.6% 매핑 |
| `sec.filings` (한국 ADR) / `sec.lei` (GLEIF KR) | 1,857 / 2,700 | LEI 매칭 120 |
| `vec.chunks` (DART + Wikipedia) | 748,812 | embedding backfill 진행 중 |
| Neo4j Company / Person / NewsEvent | 12,914 / 14,536 / 85 | 동명이인 2,171 분리 |
| Neo4j SUBSIDIARY_OF / EXECUTIVE_OF / MAJOR_SHAREHOLDER_OF | 8,661 / 33,064 / 12,548 | 시점(snapshot) + source 부여 |

### Auto 도메인 (HYUNDAI/KIA/GENESIS/TESLA × 2020–2024)

| 영역 | 적재량 | 비고 |
|---|---:|---|
| `auto.master_manufacturers` | 22,143 | NHTSA vPIC 12K + Wikidata mfr 10K (QID 10,027 매핑) |
| `auto.master_vehicle_models` | 6,729 | vPIC + Wikidata 모델 |
| `auto.master_vehicle_variants` | 237 | HYUNDAI/KIA/GENESIS/TESLA × 2020–2024 |
| `auto.events_recalls` (NHTSA) | 219 | 모두 manufacturer_id / 92% model·variant 매핑 |
| `auto.events_complaints` (NHTSA) | 16,005 | 100% mfr / 97% model·variant 매핑 |
| `auto.spec_measurements` (vPIC Canadian Specs) | **223** | 32 variants × 7 keys (length/width/height/wheelbase/track/curb) |
| `auto.components` (AI Hub 71347 + 578) | **13** | Motor-Reducer / Battery Pack / 도어/그릴/.../휀더 등 |
| `vec.chunks` (auto: nhtsa + aihub + datagokr_kotsa) | **16,242 / 모두 embedded** | manufacturer/model/variant 메타 필터 가능 |
| `bridge.corp_entity` (suppliers 포함) | **4,833** | Wikidata QID 1 + name 2 + supplier 4,830 (4 가 corp_code 매핑) |
| Neo4j Manufacturer / Model / Variant / Recall | 22,143 / 6,729 / 237 / 219 | `AFFECTED_BY` 208 |
| Neo4j System / Module / Part | (load-auto-all 후) | Level 3 / 4 / 5 — `system_taxonomy.yaml` 19 시스템 |
| Neo4j Supplier / SUPPLIED_BY | (manual seed 후) | `supplier_seed.yaml` 19 공급사 × 46 매핑 (LG에너지솔루션·한온·만도·Bosch …) |
| Neo4j RECALL_OF / CONTAINS_COMPONENT | (deterministic + AI Hub) | recall.component_text 정규화 매칭 + AI Hub 71347 → Ioniq/Kona/Niro |
| Neo4j Standard / Plant / Complaint | (seed 후) | `standards.yaml` 22 + `plants.yaml` 18 + NHTSA complaints |
| `auto.staging_relations` (P3 LLM) | extract-auto-p3 후 | SUPPLIED_BY / RECALL_OF 후보 — P4 검증 후 그래프 적재 |

---

## 2. 핵심 특징

- **멀티도메인** — `finance` + `auto` + `cross_domain` 3 모드. 도메인은 hint 또는 키워드 자동 라우팅 (`src/autograph/policy.py::route_domain`). 단일 에이전트가 두 도메인 + 둘의 교차 추론을 한 turn 안에 처리
- **금융 도메인** — DART 공시 / KRX 마스터 / ECOS / Wikidata / Wikipedia / SEC EDGAR / GLEIF / 연합뉴스 RSS / KCGS ESG → 코스피200+코스닥100 대상
- **자동차 도메인** — NHTSA vPIC/Recalls/Complaints / Wikidata (manufacturers/models/suppliers) / (옵션) car.go.kr / KATRI / KNCAP / 한국교통안전공단 수리검사. BOM Level 0~5 — Manufacturer → Model → Variant → System(L3) → Module(L4) → Part(L5, 리콜·LLM 출처에서 부분 커버). Level 6(소재·공법)은 PRD non-goal
- **3-Store 하이브리드** — Neo4j(관계) + PostgreSQL(수치·메타·벡터) + (옵션) Qdrant — 청크 100만 이하는 pgvector 통합 운영
- **Multi-Agent + Planning (LangGraph)** — Triage / Planner / Supervisor / Workers / Validator / Synthesizer 역할 분리 [PRD §7.5](./PRD.md#75-multi-agent--planning-상세-설계-langgraph)
- **채팅형 UI + 대화 히스토리** — thread 기반 multi-turn [PRD §7.6](./PRD.md#76-web-ui-채팅형--대화-히스토리-multi-turn)
- **Deterministic-first 추출** — XBRL 재무·지배구조는 정형 직매핑 (0% LLM), 서술형 관계만 selective LLM [PRD §6.5](./PRD.md#65-추출-전략-v1v2-혼합-deterministic-first--selective-llm)
- **LLM 어댑터 패턴** — `LLMClient` 단일 인터페이스, `LLM_PROVIDER` 한 줄로 백엔드 교체
- **한국어 자체 임베딩** — BGE-M3 + BGE-Reranker (GPU 자체 호스팅)
- **Entity Resolution 마스터** — corp_code 를 단일 키로 wikidata_qid / lei / cik / isin / business_no 등을 묶음. 동명이인 인물은 (name, birth_year) 분리
- **재실행 가능한 멱등 파이프라인** — raw → processed → DB. 모든 적재 `ON CONFLICT DO UPDATE` / `MERGE`. raw 만 있으면 언제든 재생성 가능

---

## 3. 아키텍처

```
[데이터 계층]
├─ Neo4j        : 기업·인물·관계 그래프 (자회사·임원·주주·뉴스·기업집단)
├─ PostgreSQL   : 재무 수치 + 마스터 + 메타 + 청크 벡터 (pgvector)
└─ (옵션) Qdrant: 청크 100만 넘으면 분리

[모델 계층]
├─ BGE-M3 (1024 dim)        : 한국어 임베딩 (GPU 0)
└─ BGE-Reranker-v2-m3       : 한국어 재랭킹 (GPU, 옵션)

[애플리케이션 계층]
├─ Ingestion Workers : DART/KRX/ECOS/Wikidata/Wikipedia/News/SEC/GLEIF/KCGS 클라이언트
├─ Loaders            : PG/Neo4j 멱등 적재 (P1 deterministic / P2 deterministic / P3 LLM / P4 cross-validate)
├─ Tools              : 사전 정의 함수 풀 (financials/graph/retrieve) — 자유 SQL/Cypher 금지
├─ Safety             : prompt_safety (XML escape + injection 감지) · cypher_guard (READ-ONLY) · language_guard
├─ Agents (LangGraph) : Triage → Planner(DAG) → Supervisor ↔ Workers(병렬: research/graph/sql/calculator)
│                       → Synthesizer → Validator (replan ≤ 2, tasks/result 자동 리셋)
│                       · Send API 병렬 디스패치 · 세션 메모리 (thread별 TTL/LRU)
│                       · checkpoint (chat.checkpoints) · streaming (SSE / st.status)
│                       · tracing (Langfuse/LangSmith)
└─ API / UI           : FastAPI /chat + /chat/stream, Streamlit 채팅 (node progress · 👍/👎/📝)

[외부 의존성]
└─ LLM Provider : OpenAI / Anthropic / 로컬 (환경변수 전환)
```

상세는 [docs/operations/agents.md](./docs/operations/agents.md) 참조.

### 저장소 역할 분리 원칙

| 저장소 | 책임 | 예시 질의 |
|---|---|---|
| Neo4j | **관계·구조** | "현대차 자회사 중 매출 1조 이상은?" |
| PostgreSQL | **정확한 수치 + 메타** | "삼성전자 2023년 매출은?" |
| pgvector / Qdrant | **의미·서술** | "삼성전자의 주요 사업 위험 요인은?" |

> 재무 수치는 절대 LLM 이 생성하지 않는다 — 반드시 PostgreSQL 조회 결과만 사용.

---

## 4. 데이터 소스

모든 데이터는 공개·합법 출처만 사용 (무단 크롤링·약관 위반 금지). 라이선스별 본문 저장 정책은 `src/autonexusgraph/ingestion/_license.py` 가 코드 레벨에서 강제.

| 데이터 | 출처 | 라이선스 | 적재 위치 |
|---|---|---|---|
| 사업보고서·공시 | DART Open API | 공공 | `data/raw/dart_bulk/` → `vec.chunks` + `fin.filings` |
| 재무제표 (XBRL) | DART | 공공 | `fin.financials` |
| 지배구조 (자회사·임원·최대주주) | DART | 공공 | Neo4j SUBSIDIARY_OF / EXECUTIVE_OF / MAJOR_SHAREHOLDER_OF |
| 상장사 마스터 | KRX | 공공 | `master.companies` |
| 거시지표 | 한국은행 ECOS | 공공 | `macro.series` |
| Wikipedia 본문·Infobox | ko.wikipedia.org | CC BY-SA | `wiki.wikipedia_pages` + `vec.chunks` (section=wikipedia_ko) |
| Wikidata 글로벌 ID·CEO·자회사 | query.wikidata.org | CC0 | `wiki.wikidata_facts` + `master.entity_map` |
| 연합뉴스 RSS | 연합뉴스 | 저작권 | `news.articles` (메타+요약만) |
| SEC EDGAR (ADR) | sec.gov | 공공 | `sec.filings` |
| GLEIF LEI | gleif.org | CC BY 4.0 | `sec.lei` + `master.entity_map` |
| KCGS ESG 등급 | cgs.or.kr | 회원 (수동) | `esg.ratings` + Neo4j Company 속성 |
| 공정위 기업집단 | data.go.kr | 공공 | (키 확보 후) Neo4j Group + BELONGS_TO_GROUP |
| KOSIS 산업 통계 | kosis.kr | 공공 | (키 확보 후) `macro.kosis_series` |
| KIPRIS 특허 | kipris.or.kr | 공공 | (키 확보 후) `ip.patents` |
| LAW.go.kr 법령 | open.law.go.kr | 공공 | (키 확보 후) `law.laws` |

**수집 범위 (1차):** 코스피 200 + 코스닥 100 약 300개사, 최근 3개 회계연도.
**범위 외 (Out-of-Scope):** 빅카인즈 본문, 나무위키(CC BY-NC-SA), 종목토론방, LinkedIn, Twitter.

### AutoGraph 데이터 소스

| 데이터 | 출처 | 라이선스 | 인증 | 적재 위치 |
|---|---|---|---|---|
| 차량 마스터·제원 (전 세계 vPIC) | NHTSA vPIC API | 공공 (US Gov) | 불필요 | `auto.master_*` |
| 리콜 캠페인 | NHTSA Recalls API | 공공 | 불필요 | `auto.events_recalls` + Neo4j Recall |
| 결함 신고 | NHTSA Complaints API | 공공 | 불필요 | `auto.events_complaints` + `vec.chunks` |
| 제조사·모델·공급사 QID·LEI·사업자번호 | Wikidata SPARQL | CC0 | 불필요 (rate limit) | `auto.master_*` + `bridge.corp_entity` |
| 자동차 리콜정보 (한국) | data.go.kr [15089863](https://www.data.go.kr/data/15089863/openapi.do) | 공공 | `DATA_GO_KR_API_KEY` | (키 확보 후) `auto.events_recalls` |
| 자동차검사관리 수리검사내역 (사고·침수·도난 차량 검사) | data.go.kr [15155857](https://www.data.go.kr/data/15155857/fileData.do) (파일 다운) | 공공 | 불필요 (파일) | `data/raw/datagokr/` → (적재 후) `auto.events_inspections` |
| 시험인증 (KATRI / 부품 인증) | bigdata-tic.kr Open API | 공공 (회원) | OAuth `BIGDATA_TIC_CLIENT_ID/SECRET` | (키 확보 후) `auto.cert_*` |
| 안전등급 (NCAP) | NHTSA SafetyRatings API | 공공 (US Gov) | 불필요 | `auto.spec_measurements` (safety.ncap.* / safety.feature.*) + Neo4j `(:VehicleVariant)-[:SAFETY_RATED_BY]->(:Standard {code:'NCAP_US'})` |
| 안전등급 (KNCAP) | car.go.kr (수동 / 별도 API) | 공공 | (지정 채널) | (후속) `auto.spec_measurements` + `:Standard {code:'KNCAP'}` |
| Euro NCAP / IIHS (옵션) | euroncap.com / iihs.org | 공공 (사용 약관) | 불필요 | (후속) `auto.spec_measurements` + `:Standard` (Euro NCAP / IIHS TSP) |

> 인증 키 부재 시 ingestion 은 graceful skip — 코드 변경 없이 `.env` 만 채우면 활성화.

---

## 5. 에이전트 도구 (사전 정의 함수 풀)

자유 SQL/Cypher/벡터 호출은 금지. LLM 은 함수명 + 파라미터만 결정. SQL injection / 그래프 폭발 / 토큰 폭발 차단 (PRD §7.5.10).

### `tools/financials.py` — PG 정형
- `lookup_company(query, limit)` — 이름·종목코드·corp_code 매칭
- `get_company_info(corp_code)` / `get_revenue(corp_code, year)` / `get_operating_income(corp_code, year)`
- `get_balance_sheet_item(corp_code, year, item)`
- `compare_companies(corp_codes, year, metric)` / `list_companies_by_market(market)`

### `tools/graph.py` — Neo4j 그래프 탐색
- `lookup_company(query, limit)` — Wikidata QID / Wikipedia title 까지 반환
- `lookup_person(name, birth_year=None)` — 동명이인 안전 매칭
- `list_subsidiaries(parent_corp_code, include_related=False, snapshot_year=None)`
- `list_parents(corp_code_or_name)` — 모회사 추적
- `get_executives(corp_code, role_contains=None, snapshot_year=None)` — `대표`, `사외이사` 등 substring
- `get_companies_of_person(name, birth_year=None, role_contains=None)`
- `get_major_shareholders(corp_code, min_pct=0.0, snapshot_year=None)`
- `find_paths(start_corp_code, end_corp_code, max_hops=3)` — 두 회사 최단 경로
- `get_subgraph(corp_code, depth=1, limit_nodes=50)`
- `list_mentioning_news(corp_code)` / `list_cooccurring(corp_code)` / `list_group_members(group_name)`

### `tools/retrieve.py` — Hybrid 검색
- `search_documents(query, top_k=8, corp_code=…, fiscal_year=…, source=…, section_contains=…)` — pgvector 코사인 + 메타 필터
- `search_by_metadata(corp_code=…, fiscal_year=…, source=…)` — 임베딩 무관, 결정적 fetch
- `get_chunk(chunk_id)` — 단일 청크 + 메타

답변은 항상 **출처(chunk_id / corp_code / rcept_no / 노드ID) + 회계연도** 명시. 불확실하면 "정보 부족" 응답.

### AutoGraph tools (`src/autograph/tools/*`)

도메인 `auto` / `cross_domain` 모드에서만 활성. workers 화이트리스트로 강제.

- **`spec.py`** — `lookup_vehicle` / `get_vehicle_info` / `get_spec` / `compare_vehicles` / `get_safety_rating` (PG SQL)
- **`graph.py`** — `lookup_vehicle_graph` / `lookup_supplier` / `list_components` / `list_recalls_affecting` / `get_suppliers_of_component` / `get_vehicles_using_component` / `find_vehicle_component_paths` (Cypher 템플릿 `auto_*` 경유)
- **`retrieve.py`** — `search_documents_auto` / `search_by_metadata_auto` / `get_chunk_auto` (pgvector + manufacturer_id/model_id/variant_id 필터)
- **`bridge.py`** — `bridge_corp_to_entity` / `bridge_entity_to_corp` / `cross_query` (corp_code ↔ entity_id, `reviewed_status='rejected'` 제외)

---

## 6. 평가 전략

### 평가셋 구성
- 공개 벤치마크: Allganize RAG-Evaluation-Dataset-KO (금융)
- 자체 구축 Multi-hop QA — 도메인 내 100문항 + Cross-Domain 30문항
  - finance: `eval/qa_gold/gold_qa_v0.jsonl` — L1/L2/L3 — seed 30 (목표 100)
  - auto: `eval/qa_gold/gold_qa_auto_v0.jsonl` — L1/L2/L3 — seed 42 (목표 100)
  - cross: `eval/qa_gold/gold_qa_cross_v0.jsonl` — CD-L1 10 / CD-L2 8 / CD-L3 8 / CD-L4 4

### 비교 매트릭스
Vector only / Graph only / **Hybrid Agent** / SQL+Vector — 4종 × LLM 3종 = 12조합
+ Cross-Domain 은 Hybrid+Bridge 어댑터 단독 (다른 어댑터는 Bridge 미사용).

### 목표 지표

| 지표 | 목표 | 측정 도구 |
|---|---|---|
| Answer Accuracy (LLM-as-judge) | 85%+ | `eval/metrics/llm_judge.py` |
| Multi-hop 정답률 (2-hop+) | 75%+ | runner 의 `multi_hop_em/f1` subset |
| Hybrid vs Vector-only Multi-hop 격차 | +30%p | runner 의 `hybrid_vs_vector` (자동) |
| 재무 수치 Exact Match | 95%+ | `eval/metrics/em_f1.py` |
| Faithfulness (Ragas) | 90%+ | `eval/metrics/faithfulness.py` |
| 평균 latency 도메인내 / Cross | < 8초 / < 12초 | `eval/metrics/latency.py` |
| Bridge confidence ≥ 0.9 비율 | 80%+ | `eval/metrics/bridge_quality.py` |
| Main-Hop Efficiency (vector 대비) | −30%+ | `eval/metrics/main_hop_efficiency.py` |
| Confidence-Weighted Accuracy | (관찰 지표) | `eval/metrics/confidence_weighted.py` |

### DoD 자동 검증

```bash
make audit-bom-coverage   # PRD §10 DoD #5
make audit-edge-meta      # PRD §10 DoD #11
make validate-gold-qa     # qa_gold/*.jsonl lint
make eval-full            # finance 100문항
make eval-auto            # auto 100문항
make eval-cross           # CD-L1~L4 30문항
make audit-dod            # 14항 트래픽라이트 종합 리포트
```

---

## 7. 로드맵

| Phase | 상태 | 산출물 |
|---|---|---|
| 1. 인프라 | ✅ | Docker Compose, Neo4j/PG, LLM 어댑터 3종, BGE-M3 가동 |
| 2. 데이터 파이프라인 (DART/KRX/ECOS) | ✅ | corp 마스터, XBRL 184K, filings 4.6K |
| 3. 청킹·임베딩·그래프 1차 | ✅ | vec.chunks 748K, Neo4j 12K Company / 14K Person |
| 3.5 데이터 통합·정합성 | ✅ | entity_map 1.9K, Wikidata/Wikipedia/GLEIF/SEC/뉴스/KCGS 통합, ER 마스터 |
| 4. RAG 도구 + 에이전트 + UI | ✅ | tools/financials·graph·retrieve, agent 4-node + answering brief + grounding, FastAPI /chat, Streamlit UI |
| 4.1 v1/v2 안전 자산 흡수 + Validator·Replan | ✅ | temporal_normalizer, prompt_safety, cypher_guard, language_guard, query_rewriter (coreference), validator + replan loop (max 2), UI title 자동 요약 + 피드백 버튼 |
| 4.2 LangGraph StateGraph + 세션 메모리 + Fallback recovery | ✅ | 실제 StateGraph + PG/Memory checkpointer, thread별 entity TTL 메모리 (carry-over), executor 빈 결과 시 search_documents 자동 회복 |
| 4.3 LangGraph 활성화 + Streaming + Tracing | ✅ | `[agent]` extra(langfuse + langsmith), DSN 우선순위 정합성, `chat` 스키마 search_path 주입, `run_agent_stream()` + FastAPI `/chat/stream` SSE, Streamlit `st.status` node progress, tracing fail-soft |
| 4.4 Multi-Agent 분리 (Supervisor + 4 Worker) + Send API 병렬 | ✅ | Planner→DAG (PRD §7.5.3), Supervisor (의존성·순환검증·budget guard), Research/Graph/SQL/Calculator worker (PRD §7.5.2), langgraph Send 병렬 디스패치 (PRD §7.5.7), Calculator numexpr 안전 evaluator (sandbox 별도) |
| 4.5 Human-in-the-Loop interrupt (Clarification) | ✅ | 모호한 회사명 자동 감지(margin<10%), `langgraph.interrupt`로 graph pause → `/chat/resume`로 재개, Streamlit clarification dialog, 폴백환경 1순위+경고 자동 다운그레이드 (PRD §7.5.6) |
| 4.6 Cost approval interrupt + estimator | ✅ | Synthesizer 호출 비용 사전 추정 (`cost_estimator.py`, replan factor 포함). `LLM_COST_AUTO_APPROVE_USD` 초과 시 사용자 승인 받음. 거절 시 supervisor가 worker skip + synthesizer가 명시 답변. 폴백환경 자동 통과+경고 (PRD §7.5.6) |
| 4.7 Cypher 템플릿 레지스트리 + Pre-synth number guard | ✅ | 22 Cypher 템플릿 (`tools/cypher_templates.py`) — type/range/regex 검증 + bool reject, find_paths 1~5hops · subgraph d1~3 사전 등록. Synthesizer 입력에서 큰 수치 화이트리스트 (`number_guard.py`) — `[수치:N]`/`[검증불가:N]` 라벨링으로 환각 사전 차단 (PRD §7.5.9 + §7.3) |
| 4.5 P3/P4 LLM 추출 (auto) | ✅ | `autograph.extractors.run_p3` + `cross_validate` (SUPPLIED_BY/RECALL_OF 활성, 4종 wired-but-disabled). `auto.staging_relations.p4_decision` 분기로 candidate/validated/needs_review/rejected. `make extract-validate-auto`. |
| 5.0 평가 인프라 확장 | ✅ | bridge_quality / main_hop_efficiency / confidence_weighted / latency 4 메트릭 신규. `scripts/audit/{bom_coverage,edge_meta_invariants,dod_audit,validate_gold_qa}.py`. `eval/qa_gold/gold_qa_v0.jsonl` (finance 30) + `gold_qa_auto_v0.jsonl` (auto 42) + `gold_qa_cross_v0.jsonl` (CD 30) seed 완료. |
| 5.1 외부 데이터 인터페이스 | ✅ | data.go.kr (15089863/15155857), car.go.kr, KATRI(bigdata-tic), KNCAP 5 소스 ingestion + loader (graceful skip). `ontology/auto/manufactured_at_seed.yaml` (46 모델↔공장 매핑). |
| 5.2 평가 실측 + 튜닝 | 🚧 | gold seed (도내 100/100 + Cross 30) 확장 + 실제 LLM 비용으로 12 조합 매트릭스 측정 대기 (`make eval-full / eval-auto / eval-cross`). |

---

## 8. 기술 스택

| 영역 | 선택 | 사유 |
|---|---|---|
| 그래프 DB | Neo4j 5.18 | Cypher 표준, APOC |
| 벡터 DB | pgvector (PostgreSQL) | 운영 단순, 100만 청크 이하 충분 |
| 정형 DB | PostgreSQL 16 | JSONB, 시계열, ON CONFLICT UPSERT |
| 임베딩 | BGE-M3 (1024d, cosine) | 한국어 성능 + 멀티벡터 |
| 에이전트 | LangGraph | 명시적 상태 관리 |
| LLM 추상화 | 자체 어댑터 (OpenAI/Anthropic/Local) | 의존성 최소화 |
| UI | Streamlit | 빠른 프로토타이핑 |

---

## 9. 비목표 (Non-Goals)

- 실시간 주가 예측 / 매매 신호 생성
- 비상장사 데이터 (DART 미제공)
- 영문 글로벌 기업 (1차 범위 외)
- 투자 자문 (정보 제공 한정)

---

## 10. 문서

- [PRD.md](./PRD.md) — 전체 요구사항·아키텍처 정의 (AutoGraph v2.1 통합)
- [docs/autograph.md](./docs/autograph.md) — **AutoGraph 도메인 전용** 가이드 (구조 / 데이터 흐름 / 실행 순서 / 알려진 제약)
- [docs/operations/docker_setup.md](./docs/operations/docker_setup.md) — Docker 스택 가이드
- [docs/operations/data_pipeline.md](./docs/operations/data_pipeline.md) — 3-tier 멱등 파이프라인 + Step DAG + 4-pass 추출 + LangGraph 활성화
- [docs/operations/agents.md](./docs/operations/agents.md) — 에이전트 아키텍처 (도메인 라우팅 / LangGraph / replan / checkpoint / tracing / safety 가드)
- [docs/operations/rag_tools.md](./docs/operations/rag_tools.md) — 도구 카탈로그 + 시나리오
- [docs/operations/kcgs_esg_guide.md](./docs/operations/kcgs_esg_guide.md) — KCGS ESG 등급 수집 가이드
- [eval/qa_gold/README.md](./eval/qa_gold/README.md) — 평가 gold set 스키마 + 큐레이션 가이드

---

## 11. Quickstart

```bash
# 0. .env 작성 (.env.example 복사 후 DART_API_KEY 채움)
cp .env.example .env

# 1. 의존성 설치
make install

# 2. DB 컨테이너 (PG + Neo4j minimal) — 데이터 폴더 먼저:
mkdir -p ~/arsim/DB_FG/{postgres,neo4j/data,neo4j/logs,neo4j/import,neo4j/plugins}
make up
# 외부 포트:  Neo4j  31009(HTTP) / 31010(Bolt)   PG  31011(pgvector 내장)
make health

# 3. 마스터 + DART 정형 데이터
make ingest-step1     # DART corp 마스터 + KRX 상장사 + targets 매칭
make load-companies   # master.companies
make load-entity-map  # ticker/jurir_no/business_no entity_map 시드

make ingest-step2     # DART filings + 재무 + 정형 지배구조 (자회사/임원/주주)
make load-all         # PG filings + financials
make load-graph-structural   # Neo4j SUBSIDIARY_OF / EXECUTIVE_OF / MAJOR_SHAREHOLDER_OF
make load-persons     # master.persons (동명이인 분리)

# 4. 외부 보강 (Wikidata + Wikipedia)
make ingest-step3     # Wikidata SPARQL (~55% 매핑)
make load-wikidata    # entity_map 보강 + Neo4j 속성

make ingest-step4     # Wikipedia 본문 + Infobox (~93% 매핑)
make load-wikipedia
make build-wiki-chunks   # Wikipedia 본문 → vec.chunks (section=wikipedia_ko)

# 5. 뉴스 + 글로벌 보강
make ingest-step6     # 연합뉴스 RSS
make load-news ; make load-graph-news     # 멘션 + CO_MENTIONED_WITH

make ingest-sec       # SEC EDGAR (한국 ADR — CIK 매핑 회사만)
make load-sec
make ingest-gleif     # GLEIF LEI (한국 jurisdiction 2,700건)
make load-gleif

# 6. 그래프 스키마 정합성 마이그레이션 (1회, 멱등 — 변경 0 이면 이미 적용됨)
make migrate-schema

# 7. KCGS ESG (수동 CSV 다운로드 후)
make ingest-kcgs                # 보도자료 모니터 — 등급 발표 알림
# 등급 CSV 를 data/raw/kcgs/<year>/ratings.csv 에 저장 후
make load-kcgs

# 8. 임베딩 (BGE-M3 GPU 가동 후 backfill)
# 별도 터미널에서:
make serve-embeddings
# 메인 터미널에서:
make embed-chunks         # vec.chunks.embedding NULL → BGE-M3 1024d 채움

# 9. 검증
make validate-quality     # 3-way cross 검증 + data/reports/quality_<date>.md

# 10. P3 LLM 관계 추출 (embedding 완료 후)
make p3-extract-dry       # 비용 추정 — LLM 호출 0
make p3-extract           # 실제 추출 (HARD_LIMIT $1.0)
make p4-load              # P4 검증 + Neo4j 적재

# 11. LangGraph 활성화 (PRD §7.5.8 — PG checkpoint + tracing)
make install-agent        # pip install -e ".[agent]" — langgraph + langfuse + langsmith
make enable-langgraph     # 헬스체크: _HAS_LANGGRAPH + checkpointer 타입 확인
# (선택) tracing: .env 에 TRACE_BACKEND=langfuse + LANGFUSE_* 키 또는 TRACE_BACKEND=langsmith + LANGSMITH_API_KEY

# 12. API + UI 가동
make serve-api            # FastAPI :31020 — POST /chat (blocking) + /chat/stream (SSE)
pip install streamlit     # (선택) UI 의존성
make serve-ui             # Streamlit :31021 채팅 UI — st.status 노드 진행 표시

# 13. 평가 (gold 큐레이션 후)
make eval-smoke           # 3 row 빠른 검증
make eval-full            # 100문항 4 어댑터 매트릭스
```

### 도구 사용 예시

```python
from autonexusgraph.tools import (
    lookup_company, list_subsidiaries, get_executives,
    get_companies_of_person, find_paths, search_documents,
)

# 1) 회사 식별
lookup_company("삼성전자")
# → [{"corp_code": "00126380", "name": "삼성전자(주)", "stock_code": "005930",
#     "wikidata_qid": "Q20718", "wikipedia_title_ko": "삼성전자"}]

# 2) 자회사 그래프
list_subsidiaries("00126380", snapshot_year=2024, limit=10)
# → [{"child_name": "삼성디스플레이", "ownership_pct": 84.78, ...}, ...]

# 3) 인물 → 임원직 회사 매트릭스
get_companies_of_person("이재용")
# → 동명이인 모두 합쳐 반환 (회사·역할·연도)

# 4) 멀티홉 경로
find_paths("00126380", "00164779", max_hops=3)
# → 삼성전자 ↔ SK하이닉스 최단 경로

# 5) Hybrid RAG
search_documents(
    "반도체 사업 위험요인",
    corp_code="00126380",
    fiscal_year=2024,
    section_contains="위험",
    top_k=5,
)
```

크롤러는 **이어받기·실패추적·Ctrl+C 안전종료** 지원. 로더는 모두 **idempotent**. raw 만 있으면 `data/processed/` 와 DB 는 언제든 재생성 가능.

### Quickstart — AutoGraph (자동차 도메인)

AutoNexusGraph 와 동일 인프라 (PG / Neo4j / pgvector / BGE-M3) 위에 자동차 도메인만 추가.

```bash
# 0. 인프라는 AutoNexusGraph quickstart 와 공유 — 동일 docker 컨테이너에 스키마만 추가
psql -h <host> -p 31011 -U autonexusgraph -d autonexusgraph -f infra/postgres/init/07_autograph.sql
psql -h <host> -p 31011 -U autonexusgraph -d autonexusgraph -f infra/postgres/init/08_bridge.sql
psql -h <host> -p 31011 -U autonexusgraph -d autonexusgraph -f infra/postgres/init/09_vec_chunks_auto_meta.sql
psql -h <host> -p 31011 -U autonexusgraph -d autonexusgraph -f infra/postgres/init/10_autograph_bom.sql
psql -h <host> -p 31011 -U autonexusgraph -d autonexusgraph -f infra/postgres/init/11_autograph_staging.sql
# (기존 DB hot-apply 절차는 docs/operations/migrations.md 참조)
python -m autograph.loaders.neo4j_init    # CONSTRAINT/INDEX 멱등 — ontology/auto/entities.yaml SSOT

# 1. 인제스션 (.env 의 AUTO_INGEST_MAKES / AUTO_INGEST_YEAR_MIN/MAX 기반)
make ingest-auto-all                # = vpic + recalls + complaints + wikidata

# 2. P2 결정적 적재 — raw → PG → Neo4j → bridge → seed/supplier/recall→comp → chunks
make load-auto-all
# 의존 순서: neo4j-init → pg → specs → neo4j → bridge → aihub
#          → supplier-edges → seed-standards-plants → complaints-neo4j
#          → recall-components → build-chunks-auto

# 3. 청크 임베딩 (finance 와 동일 BGE-M3 backfill — generic 작업)
make embed-chunks

# 4. (선택) P3 LLM 관계 추출 — 비용 가드 dry-run 먼저
make extract-auto-p3-cost MFR_IDS=498 P3_LIMIT=50
make extract-auto-p3      MFR_IDS=498 P3_LIMIT=50 P3_HARD_LIMIT=2.0
make validate-auto-p4     # auto.staging_relations → P4 → Neo4j candidate/validated 적재

# 5. 에이전트 호출 (도메인 명시 또는 자동 판정)
python -c "from autonexusgraph.agents import run_agent;
s = run_agent('Hyundai Sonata 2024 리콜 사례', domain='auto');
print(s['answer'])"

# 6. 평가
make eval-auto                       # eval/reports/auto_<timestamp>/summary.md
```

자세한 절차·미구현 영역·회귀 안전성은 [docs/autograph.md](./docs/autograph.md). 도메인 라우팅 흐름은 [docs/operations/agents.md](./docs/operations/agents.md#도메인-라우팅-finance--auto--cross_domain).

---

## 12. 라이선스

내부 연구·개발 단계. 라이선스 미정.
