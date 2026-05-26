# PRD: 금융 도메인 GraphRAG 에이전트 시스템 v2.0

**문서 버전:** 1.2
**작성일:** 2026-05-26
**작성 목적:** 기존 시스템(WSL + 원격 Neo4j + Azure OpenAI 전용 + 일반 도메인)을 → Linux 컨테이너 기반의 멀티 LLM 지원 금융 도메인 GraphRAG 에이전트로 전면 재구축하기 위한 방향성과 요구사항 정의

---

## 1. 프로젝트 개요

### 1.1 배경

기존 시스템은 다음과 같은 제약을 가지고 운영되어 왔다:

- **개발 환경:** Windows + WSL에서 직접 실행 — 재현성·배포·협업에 한계
- **데이터베이스:** 원격 Neo4j 의존 — 네트워크 지연, 단일 장애점, 비용 부담
- **LLM 종속성:** Azure OpenAI에 강하게 결합 — 비용 협상력 약화, 멀티 클라우드 전략 불가, 모델 선택 제약
- **도메인:** 일반 RAG 예제 수준 — 실무적 가치 입증이 어려움
- **임베딩:** 외부 API 의존 — 단가 부담 + 한국어 성능 한계

이 PRD는 위 제약을 해소하면서, 동시에 **GraphRAG가 진가를 발휘할 수 있는 도메인(금융)** 으로 재정의하여, 단순 검색 RAG가 풀지 못하는 멀티홉 추론 문제를 정량적으로 입증하는 시스템을 구축하는 것을 목표로 한다.

### 1.2 프로젝트 한 줄 정의

> **"한국 상장사 공시·재무 데이터를 기반으로, 기업 간 복잡한 관계를 그래프로 추론하여 답변하는 금융 분석 GraphRAG 에이전트"**

### 1.3 핵심 변경사항 (AS-IS → TO-BE)

| 영역 | AS-IS | TO-BE | 변경 이유 |
|---|---|---|---|
| OS 환경 | Windows + WSL | Linux 서버 (Native) | 운영 환경 일관성 |
| 실행 방식 | WSL에서 직접 실행 | Docker Compose 멀티 컨테이너 | 재현성·이식성·격리 |
| Neo4j | 원격 서버 연결 | 로컬 컨테이너 | 지연 최소화, 비용 절감, 데이터 주권 |
| RDBMS | 없음 | PostgreSQL 컨테이너 추가 | 정형 데이터(재무수치) 신뢰성 보장 |
| 벡터 DB | 코드 내장/임시 | Qdrant 컨테이너 | 영속성, 성능, 운영 안정성 |
| 임베딩 | 외부 API | **BGE-M3 자체 호스팅 (GPU)** | 한국어 성능, 비용, 프라이버시 |
| **LLM** | **Azure OpenAI 전용** | **OpenAI GPT / Anthropic Claude / 로컬 LLM 어댑터 패턴** | 벤더 종속 해소, 비용·성능 최적화 |
| **데이터 정책** | **외부 데이터 접근 제한** | **공개 오픈 데이터 적극 활용** | 금융 공공 데이터 기반 시스템 구축 |
| 도메인 | 일반 예제 | **한국 금융 (DART/KRX/ECOS)** | 실용성, 차별성, 평가 명확성 |
| 그래프 스키마 | 일반 엔티티 | 금융 특화 (Company/Person/Financial 등) | 도메인 추론 정확도 |

---

## 2. 목적 및 목표

### 2.1 비즈니스 목적

1. **단순 RAG로 풀 수 없는 질문을 푼다**
   - "삼성전자 자회사 중 반도체 의존도가 높은 곳은?"
   - "이재용 회장이 등기임원인 회사들의 매출 합계는?"
   - 이런 질문은 의미 검색만으로는 불가능하며, 관계 그래프 탐색이 필수

2. **벤더 종속을 끊는다**
   - Azure 외에 OpenAI/Anthropic/로컬 LLM 어느 것으로든 즉시 전환 가능
   - 동일한 평가셋에서 LLM별 성능·비용 비교 가능

3. **재현 가능한 시스템을 만든다**
   - `docker compose up` 한 줄로 전체 스택 재현
   - 데이터 수집부터 평가까지 자동화

### 2.2 기술 목표

| 목표 | 측정 지표 | 목표치 |
|---|---|---|
| 한국어 금융 RAG 정확도 | Answer Accuracy (LLM-as-judge) | 85%+ |
| Multi-hop 추론 성공률 | 2-hop 이상 질문 정답률 | 75%+ |
| Hybrid 우위 입증 | Vector 단독 대비 Multi-hop 정답률 향상 | +30%p 이상 |
| LLM 교체 비용 | Provider 변경 시 코드 수정량 | 환경변수 1줄 |
| 시스템 응답 시간 | 평균 응답 latency | < 8초 |
| 환각률 | Faithfulness (Ragas) | 90%+ |

### 2.3 비목표 (Non-Goals)

- 실시간 주가 예측 / 매매 신호 생성 (책임 이슈)
- 비상장사 데이터 (DART 미제공)
- 영문 글로벌 기업 지원 (1차 범위는 한국 상장사)
- 투자 자문 (정보 제공 한정)

---

## 3. 데이터 정책 (변경 핵심)

### 3.1 오픈 데이터 활용 원칙

기존의 "외부 데이터 접근 제한" 정책을 해제하고, **공개된 정부·공공·오픈소스 데이터를 적극 수집·활용** 한다. 모든 데이터는 다음 기준을 충족해야 한다:

- ✅ 공공기관·공식 API 또는 라이선스 명시된 오픈 데이터
- ✅ 상업적·연구 목적 사용 허용 라이선스
- ✅ 출처 명시 가능
- ❌ 무단 크롤링, 약관 위반 수집 금지
- ❌ 개인정보·민감정보 포함 데이터 금지

### 3.2 데이터 소스

#### 구축용 (Knowledge Source)

| 데이터 | 출처 | 형태 | 용도 |
|---|---|---|---|
| **사업보고서/공시** | DART Open API | XML/PDF | 본문 임베딩 + 관계 추출 |
| **재무제표 (XBRL)** | DART | 정형 데이터 | PostgreSQL 정량 노드 |
| **상장사 마스터** | KRX 정보데이터시스템 | CSV | 종목·업종 분류 |
| **거시지표** | 한국은행 ECOS API | 시계열 | 거시 컨텍스트 노드 |
| **기업 지배구조** | DART 지배구조보고서 | 정형 | 임원·자회사 관계 |

#### 평가용 (Benchmark)

| 데이터셋 | 출처 | 용도 |
|---|---|---|
| Allganize RAG-Evaluation-Dataset-KO (금융) | Hugging Face | 한국어 금융 RAG 표준 벤치마크 |
| KorFin-MRC / 금융 MRC | AI Hub | 금융 기계독해 평가 |
| 자체 구축 Multi-hop QA (100문항) | 직접 생성 + LLM 보조 | GraphRAG 핵심 검증 |
| FinanceBench (영문 참고) | 공개 | 글로벌 비교용 (선택) |

### 3.3 수집 범위 (1차)

- **대상:** 코스피 200 + 코스닥 100 = 약 300개사
- **기간:** 최근 3개 회계연도
- **문서 유형:** 사업보고서, 분기/반기보고서, 주요사항보고서
- **거시지표:** 기준금리, 환율, 주요 산업지표

---

## 4. 시스템 아키텍처 방향성

### 4.1 컨테이너 토폴로지

전체 시스템은 단일 Linux 호스트 위에서 Docker Compose로 오케스트레이션되며, 모든 컴포넌트가 격리된 컨테이너로 구동된다.

```
[데이터 계층] (minimal — 2 DBs)
├─ Neo4j 5.18    : 기업·인물·관계 그래프
└─ PostgreSQL 16 : 재무 수치 / 회사 마스터 / 평가 QA / 채팅 히스토리 /
                   LangGraph checkpoint / **문서 청크 벡터(pgvector 확장)**

  └─ (옵션) Qdrant      : 청크 수 100만 넘을 때 분리 — 현 규모(~45K)는 pgvector 충분
  └─ (옵션) Redis       : 분산/다중 worker 단계에서 캐시·queue

[모델 계층]
├─ BGE-M3        : 한국어 임베딩 (GPU)
└─ BGE-Reranker  : 한국어 재랭킹 (GPU)

[애플리케이션 계층]
├─ Ingestion Worker : 데이터 수집·전처리·그래프 추출 배치
├─ API (FastAPI)    : 에이전트 오케스트레이션
└─ Web (Streamlit)  : 사용자 인터페이스

[외부 의존성]
└─ LLM Provider : OpenAI / Anthropic / 로컬 (택1, 환경변수 전환)
```

### 4.2 데이터 흐름 방향성

1. **수집 단계:** DART/KRX/ECOS API → Raw 데이터 저장 → PostgreSQL 정형 적재
2. **전처리 단계:** 문서 청킹 → BGE-M3 임베딩 → Qdrant 저장
3. **그래프 구축 단계:** LLM 기반 엔티티/관계 추출 + 정형 데이터 직접 매핑 → Neo4j 적재
4. **질의 단계:** 사용자 질문 → 에이전트 라우팅 → Vector/Graph/SQL 도구 선택 → LLM 답변 합성
5. **평가 단계:** 평가 QA 실행 → 결과 PostgreSQL 적재 → 대시보드 표시

### 4.3 그래프 vs 정형 DB 역할 분담

GraphRAG의 흔한 실패 원인은 **정량 데이터를 그래프에 욱여넣는 것**이다. 본 시스템은 다음과 같이 명확히 분리한다:

- **Neo4j (관계 중심):** 누가 누구의 자회사인가, 누가 어느 회사의 임원인가, 어떤 산업에 속하는가 등 **관계 탐색이 필요한 정보**
- **PostgreSQL (수치 + 의미 중심):** 매출액·영업이익·자산 등 **정확한 숫자** + 사업개요·위험요인·경영전략 등 **자연어 본문 청크 + 벡터(pgvector HNSW 인덱스)**
- (옵션) **Qdrant**: 청크 수 100만 넘으면 분리. 현 단계는 PG 통합으로 운영 단순화.

질의 시 에이전트는 두 저장소(필요 시 Qdrant 까지)를 **상황에 따라 조합 호출**한다.

---

## 5. LLM 추상화 전략 (Azure 종속 제거)

### 5.1 방향성

기존 코드 전반에 산재한 `AzureOpenAI`, `AzureChatOpenAI` 직접 호출을 **모두 제거**하고, 단일 추상 인터페이스 `LLMClient`를 통해서만 LLM에 접근하도록 강제한다.

### 5.2 어댑터 패턴 원칙

- **하나의 인터페이스, 다수의 구현체**
  - OpenAI 어댑터: GPT-4o, GPT-4o-mini 등
  - Anthropic 어댑터: Claude Sonnet/Opus 등
  - Local 어댑터: vLLM/Ollama 기반 오픈 모델

- **전환 비용 최소화**
  - 환경변수 `LLM_PROVIDER` 한 줄 변경으로 백엔드 교체
  - 비즈니스 로직 코드는 LLM 종류를 알 필요 없음

- **공통 기능 표준화**
  - 일반 채팅, 스트리밍 응답, JSON 구조화 출력 — 3가지 기본 메서드로 통일
  - 토큰 사용량·비용 로깅 표준화

### 5.3 LLM 용도별 권장 매핑

| 용도 | 권장 모델 | 이유 |
|---|---|---|
| 엔티티/관계 추출 (배치) | GPT-4o-mini 또는 로컬 LLM | 대량 처리, 비용 우선 |
| Cypher 쿼리 생성 | Claude Sonnet 또는 GPT-4o | 구조화 추론 강점 |
| 최종 답변 합성 | Claude Sonnet 또는 GPT-4o | 한국어 품질, 추론력 |
| 평가 (LLM-as-judge) | GPT-4o 고정 | 평가 일관성 |

→ 운영자가 각 용도에 다른 모델을 매핑할 수 있도록 설정 분리

---

## 6. 도메인 변경에 따른 코드 재구성 방향

기존 v1, v2, web 코드는 다음 방향으로 전면 재구성된다:

### 6.1 v1 (기본 RAG) → 금융 Vector RAG
- 일반 문서 → DART 사업보고서로 데이터 소스 교체
- 단순 임베딩 → BGE-M3 + Reranker 2단계 검색
- 단일 검색 → 회사·연도·섹션 필터링 메타데이터 강화

### 6.2 v2 (GraphRAG) → 금융 GraphRAG
- 일반 엔티티 추출 → 금융 특화 스키마(Company/Person/Industry/Financial)로 제한
- 자유 관계 → 사전 정의된 관계 타입(SUBSIDIARY_OF, EXECUTIVE_OF 등)으로 제약
- 자유 Cypher 생성 → 스키마 인지(Schema-aware) Cypher 생성으로 정확도 향상
- 정형 데이터(재무 수치)는 그래프가 아닌 PostgreSQL 직접 조회로 분리

### 6.3 web (UI) → 금융 에이전트 UI (채팅형)
- **단발 검색창 → 채팅형 대화 인터페이스 (Multi-Turn, thread 기반 히스토리)** — 상세 §7.6
- 평문 응답 → 출처 인용, 그래프 시각화(서브그래프), 재무 차트 동반
- Azure 연결 설정 화면 → LLM Provider 선택 UI (또는 운영 환경변수로 위임)
- 평가 모드 추가 → 벤치마크 일괄 실행 및 비교 대시보드
- 진행 중인 에이전트(Planner/Graph/Validator…) 실시간 표시, 비용 누적 표시

### 6.4 공통 변경
- 모든 `AzureOpenAI`/`AzureChatOpenAI` 호출 제거 → `LLMClient` 인터페이스 사용
- 모든 외부 API 키·엔드포인트 → `.env` 중앙 관리
- 모든 DB 연결 → 컨테이너 서비스명 기반 (`neo4j:7687`, `postgres:5432`)
- 모든 임베딩 호출 → BGE-M3 자체 호스팅 엔드포인트로 변경

### 6.5 추출 전략: v1/v2 혼합 (Deterministic-first + Selective LLM)

기존 BNT_ONTOLOGY 시스템의 두 트랙 (v1=LLM 4-pass 전수 추출, v2=deterministic-first + selective LLM) 의 학습을 흡수하여, FinGraph 는 **결정론 우선 + LLM 보조** 의 4단계 파이프라인으로 통일한다.

| Pass | 입력 | 방식 | 산출물 | LLM 비중 |
|---|---|---|---|---|
| **P1 (Det)** | DART XBRL 재무제표 | 직접 매핑 (Spec 기반) | PG 테이블 (`financials`) | 0% |
| **P2 (Det)** | DART 지배구조 보고서 (정형 JSON/XML) | 직접 매핑 | Neo4j 노드/관계 (`Person`, `Company`, `EXECUTIVE_OF`, `SUBSIDIARY_OF`, ownership_pct) | 0% |
| **P3 (LLM)** | 사업보고서 본문 (자연어) | Schema-aware LLM 추출 (Company/Person/Industry 한정) | Neo4j 관계 후보 (`PARTNER_OF`, `COMPETES_WITH`, `INVESTED_IN`) | 100% |
| **P4 (LLM-aug)** | P3 산출 + P1/P2 결과 | 정형 cross-check, 충돌 시 정형 우선 | Neo4j 확정 관계 (validated) | 보조 (검증만) |

**원칙:**
- 정량 수치(매출/영업이익)는 P1 으로 100% 결정론. LLM 추출 금지.
- 지배구조(임원/자회사/지분율)는 P2 로 정형 매핑. LLM 보조도 불필요.
- "왜 협업?" "어떤 위험?" 같은 서술형 관계만 P3 LLM 추출 → 비용 절감
- P4 가 P3 의 LLM 환각을 P1/P2 정형 데이터로 cross-validate (예: "A 가 B 의 자회사라고 LLM 이 추출했는데 P2 에 없음 → 폐기 또는 review_inbox 로")

**LLM 비용 가드:**
- 배치 P3 는 경량 모델 (GPT-4o-mini / 로컬) — PRD §5.3 매핑 따름
- 청크 단위 호출 병렬도 조절 (`INGEST_PASS3_PARALLEL`, 기본 2)
- 회사·연도·섹션별 캐시 (`processed/extracted/<corp_code>/<rcept_no>.jsonl`)

---

## 7. 에이전트 동작 방향성

### 7.0 설계 채택: Multi-Agent + Planning (LangGraph)

본 시스템은 단일 ReAct 에이전트가 아닌 **역할 분리된 다중 에이전트 + 명시적 계획 수립** 구조를 채택한다. 상세는 §7.5 참조.

- **왜 단일 LLM이 부족한가:** 도구 선택 실패, 컨텍스트 오염, 추론 깊이 부족
- **왜 LangGraph인가:** 명시적 StateGraph, 분기·병렬·재시도·체크포인트 내장, Human-in-loop 네이티브 지원
- **핵심 가치:** 디버깅 가능성, 재현성, 검증 분리 — 금융 도메인의 정확성·감사 가능성 요구에 부합

### 7.1 라우팅 원칙

사용자 질문을 받으면 에이전트는 다음 판단을 한다:

1. **단순 사실 질문** (예: "삼성전자 2023년 매출은?")
   → PostgreSQL 직접 조회 (가장 빠르고 정확)

2. **의미·서술형 질문** (예: "삼성전자의 주요 사업 위험 요인은?")
   → Vector RAG (Qdrant + Reranker)

3. **관계·구조 질문** (예: "현대차 자회사 중 매출 1조 이상은?")
   → Graph RAG (Neo4j) + PostgreSQL 조합

4. **복합 멀티홉 질문** (예: "이재용이 임원인 회사들의 합산 영업이익은?")
   → Graph 탐색 + PostgreSQL 집계 + Vector 보완

### 7.2 도구(Tool) 추상화

에이전트는 다음 도구들을 보유하며, LLM이 판단해 호출한다:

- `search_documents(query, filters)` — 벡터 검색
- `query_graph(cypher_intent)` — 그래프 탐색
- `query_financials(company, year, metric)` — 재무 정확값 조회
- `lookup_company(name_or_ticker)` — 회사 식별
- `get_subgraph(entity, depth)` — 시각화용 서브그래프

### 7.3 답변 신뢰성 보장 원칙

- **재무 수치는 절대 LLM이 생성하지 않는다** — 반드시 PostgreSQL 조회 결과만 사용
- **모든 답변은 출처 명시** — 문서 ID, 페이지, 그래프 노드 ID
- **불확실한 경우 "정보 부족"으로 응답** — 환각 방지
- **시점 정보 필수** — "2023년 기준" 등 항상 회계연도 명시

---

## 7.5 Multi-Agent + Planning 상세 설계 (LangGraph)

§7의 라우팅·도구·신뢰성 원칙을 구현하는 실제 아키텍처. "하나의 똑똑한 에이전트보다, 역할이 분명한 여러 전문가 + 명시적 계획"이 본 시스템의 설계 철학이다.

### 7.5.1 설계 원칙

- **역할 분리 (Single Responsibility):** 각 에이전트는 한 가지만 한다. 디버깅·평가·교체가 명확.
- **명시적 계획 (Plan-and-Execute):** LLM이 즉흥적으로 도구를 호출하지 않는다. Planner가 먼저 DAG를 만들고, 그 뒤에 실행한다.
- **검증 분리:** Validator가 독립적으로 결과를 검증한다 → 신뢰성·환각 방지.
- **재계획 가능:** 실패 시 Planner로 되돌아간다. 무한 루프 방지를 위해 `replan_count ≤ 2`.
- **상태 명시 (StateGraph):** 모든 단계가 LangGraph State에 기록 → 완전한 추적성, 체크포인트, 재개.
- **재무 수치는 결정론적:** LLM 환각 원천 차단. SQL/Graph 조회 결과만 사용.

### 7.5.2 에이전트 구성 (7~9종)

| 에이전트 | 책임 | LLM 권장 | 도구 |
|---|---|---|---|
| **Triage** | 의도 분류, 모호성 감지, 회사 식별, 시점 추출 | 경량 (Haiku / GPT-4o-mini) | `lookup_company` |
| **Planner** | 태스크 분해 → DAG 생성, 워커 지정, 의존성 정의 | 고급 (Sonnet / GPT-4o) | 없음 (순수 추론) |
| **Supervisor** | 의존성 충족된 다음 태스크 선택, 병렬 디스패치, 상태 추적 | 경량 | 없음 (라우팅만) |
| **Research** | 벡터 검색 + 재정렬, 문서 인용 | 경량 | `vector_search`, `rerank` |
| **Graph** | Cypher 템플릿 + 파라미터 채우기, 관계 탐색 | 고급 | `cypher_query`, `get_subgraph` |
| **SQL** | 사전 정의 함수 풀 호출 (자유 SQL 금지) | 경량 | `get_financials`, `compare_companies` |
| **Calculator** | 재무 계산·통계, Python sandbox | 경량 + 코드 실행 | `python_exec` (격리) |
| **Validator** | Citation 체크, 수치 일치성, 논리 일관성, 완전성 | 경량 | `check_citation`, `verify_number` |
| **Synthesizer** | 최종 답변 작성, 출처 태깅, 시각화 결정 | 고급 | `generate_chart` |

### 7.5.3 LangGraph State 스키마

모든 노드가 공유하는 단일 `AgentState` (TypedDict). 핵심 필드:

```
AgentState:
  messages              : list[Message]            # 대화 히스토리 (append-only)
  user_query            : str                      # 원본 질문
  clarified_query       : str                      # Triage 산출
  identified_entities   : dict                     # companies/persons/time_range
  plan                  : dict                     # Planner 산출
    └─ tasks: list[{id, agent, intent, depends_on, status}]
  current_task_id       : str
  task_results          : dict[task_id, result]    # append-only
  retrieved_documents   : list
  graph_results         : dict
  sql_results           : dict
  calculations          : dict
  validation_status     : "pending" | "passed" | "failed"
  validation_issues     : list
  replan_count          : int                      # max 2
  final_answer          : str
  citations             : list
  visualizations        : list
  metadata              : {tokens, cost, latency}
  trace_id              : str
```

**원칙:** 불변성(노드는 변경하지 않고 새 값 반환), 점진적 누적(리스트는 append-only), 체크포인트(각 단계 PostgreSQL 저장).

### 7.5.4 흐름

```
User Query
   ↓
[1] Triage (intake & clarify)
   ↓
[2] Planner (task DAG 생성)
   ↓
[3] Supervisor (router) ──┬─→ Research ─┐
                          ├─→ Graph    ─┤
                          ├─→ SQL      ─┤  (의존성 없는 워커는 병렬, Send API)
                          └─→ Calc     ─┘
                                    ↓
                          [4] Validator
                                    ↓
                          ┌─ failed → [Replan] (count<2)
                          └─ passed → [5] Synthesizer
                                              ↓
                                       Final Answer + Citations + Viz
```

### 7.5.5 Replan & 무한 루프 방지

다음 경우 Planner로 되돌아감:
- Validator가 `failed` 반환
- Worker가 "데이터 없음" 보고
- 중간 결과가 예상과 크게 다름

`replan_count` 최대 2회. 초과 시 부분 답변 + "정보 부족" 명시 반환.

### 7.5.6 Human-in-the-Loop

LangGraph `interrupt` 활용 시점:
- **Clarification 필요:** 모호한 회사명 ("삼성" → 삼성전자/SDS/...) — Triage 단계
- **고비용 작업 전:** "이 작업은 OpenAI API $0.50 소요됩니다" — Planner 산출 후
- **민감 결정:** "이 답변을 외부 보고서로 사용하시겠습니까?" — Synthesizer 직전

### 7.5.7 병렬 실행

Planner DAG에서 의존성 없는 태스크는 LangGraph `Send` API로 동시 디스패치. 예시:
```
질문: "삼성전자와 LG전자의 최근 5년 매출 비교"
T1: 삼성전자 매출 ──┐
                  ├─→ T3: 비교·차트
T2: LG전자 매출   ──┘
```
T1·T2 병렬 실행 → latency 단축.

### 7.5.8 Checkpoint & Resume

모든 State는 PostgreSQL에 체크포인트 저장:
- 시스템 중단 시 마지막 노드부터 재개
- 사용자가 "다시 계산" 요청 시 처음부터 재실행
- 평가·디버깅 시 시점별 State 추출

### 7.5.9 Cypher 안전성: 템플릿 + 파라미터

자유 Cypher 생성은 환각·SQL Injection 유사 위험. 대신 **사전 정의된 템플릿**에 LLM이 파라미터만 채움:

```cypher
-- 템플릿
MATCH (c:Company {name: $name})-[:SUBSIDIARY_OF*1..2]->(p:Company)
WHERE r.ownership_pct >= $threshold
RETURN p.name, p.corp_code
```
LLM 출력: `{name: "현대자동차", threshold: 50}` (JSON Schema 강제).

### 7.5.10 SQL 안전성: 함수 풀

자유 SQL 금지. 사전 정의된 함수만 호출 가능:
- `get_revenue(company_id, year)`
- `get_operating_income(company_id, year)`
- `compare_companies(company_ids, metric, years)`
- `aggregate_by_industry(industry_code, metric, year)`

READ-ONLY DB 사용자로 연결. SQL Injection 원천 차단.

### 7.5.11 Tracing & 보안

- **Tracing:** Langfuse 또는 LangSmith 통합 필수. 모든 노드 진입/종료 span, State 변화 시각화, 실패 케이스 자동 수집.
- **Prompt Injection 방어:** 사용자 입력과 시스템 프롬프트 분리, 검색 문서는 명확한 구분자(`<document>` 등) 사용, 의심 패턴 사전 차단.
- **도구 권한:** 각 에이전트는 자기 도구만 호출 가능 (LangGraph 노드 단위 제한).
- **Python sandbox:** Calculator는 네트워크·파일시스템 격리 (e2b / daytona / 자체 Docker).

### 7.5.12 프롬프트 엔지니어링

- 에이전트별 분리된 System Prompt (역할 + 능력 + 제약 + Few-shot + JSON Schema)
- 각 에이전트는 **자기 일에 필요한 State만** 받음 (전체 messages 전달 X → 토큰 절약·혼란 방지)
- 모든 의사결정 노드는 JSON 출력 강제 (OpenAI Structured Outputs / Anthropic Tool use / 로컬 Outlines)

---

## 7.6 Web UI: 채팅형 + 대화 히스토리 (Multi-Turn)

§6.3 의 web 재구성 방향을 구체화. 단발 검색창이 아니라 **연속 대화(채팅)** 가 기본 인터랙션.

### 7.6.1 인터랙션 모델

- 좌측: 대화 목록 (`thread_id` 단위) — 새 대화 / 기존 대화 선택 / 삭제
- 중앙: 채팅 메시지 스트림 (`st.chat_message` / `st.chat_input`)
- 우측 (또는 메시지 하단 아코디언): 출처 패널 — 인용 문서, 그래프 서브뷰, SQL 결과 표, 시각화

### 7.6.2 Multi-Turn 동작

LangGraph `thread_id` 기반 대화 격리. 각 turn 은 다음 흐름:

```
turn N:
  user message + 이전 turn 의 final State (entities, recent_tasks, citations 미니 요약)
  → Triage (이번 turn 의 query 명확화, "이 회사들" 같은 대명사 해소)
  → Planner (이전 결과 reuse 우선; 변경된 부분만 재실행)
  → ... (이후 §7.5 흐름과 동일)
  → Synthesizer (이번 turn 답변 + 이전 turn 과의 연결 명시)
```

핵심 패턴:
- "위에서 답한 회사 중 매출 1조 이상은?" → Planner 가 이전 turn 의 `task_results` 를 P2 로 reuse
- "방금 그 차트를 산업별로 다시" → Synthesizer 가 동일 데이터 + 새 그루핑으로 재생성
- "처음부터 다시" → 명시 시 새 `thread_id` 또는 State reset

### 7.6.3 히스토리 저장

- LangGraph `CheckpointSaver` → PostgreSQL (`langgraph_checkpoints` 테이블 자동 생성)
- 별도 `conversations` / `messages` 테이블 (UI 표시·검색용 정규화 view):
  ```sql
  conversations(id, thread_id, title, created_at, updated_at, user_id)
  messages(id, conversation_id, turn_idx, role, content, citations_json, viz_json, created_at)
  ```
- `title` 은 첫 user message 의 첫 LLM 호출로 자동 요약 생성 (5단어 이내)
- 검색: `messages.content` 전문 검색 (PG `tsvector` 인덱스)

### 7.6.4 컨텍스트 윈도우 관리

장기 대화에서 토큰 폭증 방지:
- Triage 단계에서 **이번 turn 에 실제 필요한 prior context 만 추출** (최근 N개 turn + 관련된 entities/tasks)
- 8 turn 초과 시 자동 요약 (sliding window summary 를 별도 메시지로 주입)
- 사용자가 명시적으로 "위 내용 잊어" 요청 시 thread fork 또는 reset

### 7.6.5 사용자 경험

- 토큰 사용량 / 비용 실시간 표시 (turn 당 + 누적)
- 진행 중인 에이전트 표시 (Planner → Graph → Validator ...) — LangGraph stream 으로 노드 진입 시 chip 업데이트
- 답변 중 "출처 부족" / "재계획" 발생 시 명시 (사용자가 신뢰도 판단)
- 답변 후 피드백 버튼 (👍/👎/📝 의견) → 평가 데이터 적재

### 7.6.6 구현 스택

- **Streamlit** (`streamlit>=1.35`): chat_message/chat_input 네이티브 지원
- **PostgreSQL**: LangGraph checkpoint + conversations/messages 테이블
- **FastAPI** (선택): UI 와 에이전트 분리 시 SSE/WebSocket streaming 엔드포인트
- 차트: Plotly (인터랙티브 재무 시계열), pyvis (서브그래프 시각화)

---

## 8. 평가 및 검증 전략

### 8.1 평가 데이터셋 구성

- **공개 벤치마크:** Allganize 금융 도메인 → 기본 성능 baseline
- **자체 구축 QA 100문항:**
  - Level 1 (단순 사실, 30개): 단일 회사·단일 수치
  - Level 2 (2-hop, 40개): 회사-회사, 회사-인물 관계
  - Level 3 (3-hop+, 30개): 다중 관계 + 집계
- **각 QA는 정답·정답 출처·필요 hop 수 메타데이터 포함**

### 8.2 비교 실험 매트릭스

다음 조합을 모두 평가하여 GraphRAG의 우위를 정량 입증한다:

| 시스템 | Level 1 | Level 2 | Level 3 | 종합 |
|---|---|---|---|---|
| Vector RAG only | 측정 | 측정 | 측정 | - |
| Graph RAG only | 측정 | 측정 | 측정 | - |
| **Hybrid Agent** | 측정 | 측정 | 측정 | - |
| SQL+Vector (No Graph) | 측정 | 측정 | 측정 | - |

→ **Hybrid가 모든 레벨에서 우월하거나, 최소 Level 2/3에서 큰 폭 우위**를 보여야 함

### 8.3 평가 지표

- **Retrieval:** Recall@k, MRR
- **Answer:** Accuracy (LLM-as-judge), Exact Match (수치 질문)
- **Faithfulness:** Ragas 기반 환각 측정
- **Operational:** Latency, 토큰 비용, LLM별 비용 비교
- **Multi-hop Specific:** Hop-level Success Rate

### 8.4 LLM 비교 평가

동일 평가셋을 GPT-4o, Claude Sonnet, 로컬 LLM에서 각각 실행하여 다음을 보고:

- 정확도 차이
- 비용 차이 (질문당 평균 토큰·USD)
- 응답 시간 차이
- 한국어 자연스러움 정성평가

→ **어떤 LLM이 어떤 용도에 적합한지 가이드라인 도출**

---

## 9. 단계별 로드맵

### Phase 1: 인프라 구축 (1주차)
- Docker Compose 전체 스택 구성
- Neo4j / PostgreSQL / Qdrant 부트스트랩
- BGE-M3 GPU 컨테이너 구동 확인
- LLM 어댑터 3종 구현 및 단위 테스트

### Phase 2: 데이터 파이프라인 (2주차)
- DART API 수집 모듈
- KRX/ECOS 수집 모듈
- PostgreSQL 스키마 적재
- 문서 청킹 + 임베딩 + Qdrant 적재
- LLM 기반 엔티티/관계 추출 + Neo4j 적재

### Phase 3: RAG 파이프라인 (3주차)
- Vector RAG (v1 재구성)
- Graph RAG (v2 재구성, 스키마 인지 Cypher)
- 정형 SQL 조회 도구
- 도구 단위 동작 검증

### Phase 4: 에이전트 + UI (4주차) — Multi-Agent 단계적 도입
- **Phase 4A:** Triage + Supervisor + 3 Worker(Research/Graph/SQL) 선형 흐름
- **Phase 4B:** Planner Agent 도입 — DAG 기반 태스크 디스패치, 의존성 관리
- **Phase 4C:** Validator + Replan 루프 — Citation 강제, 환각 검증
- **Phase 4D:** Send API 병렬 실행, Calculator + Python sandbox, Checkpoint/Resume
- **Phase 4E:** Streamlit UI + 그래프 시각화, Tracing(Langfuse/LangSmith), Human-in-loop
- 상세 설계는 §7.5 참조

### Phase 5: 평가 및 튜닝 (5주차)
- 자체 평가 QA 100문항 구축
- 시스템 4종 × LLM 3종 = 12조합 평가 실행
- 결과 대시보드 + 분석 리포트
- 프롬프트·청킹·검색 파라미터 튜닝

---

## 10. 성공 기준 (Definition of Done)

본 프로젝트는 다음 조건을 모두 충족할 때 성공으로 간주한다:

1. ✅ `docker compose up` 한 줄로 전체 시스템 기동 가능
2. ✅ LLM Provider를 환경변수만으로 OpenAI/Anthropic/로컬 간 전환 가능
3. ✅ 코스피200+코스닥100 기업의 최근 3년 데이터가 그래프·정형·벡터 3개 저장소에 적재됨
4. ✅ 자체 평가 QA 100문항에서 Hybrid Agent가 Vector 단독 대비 Multi-hop에서 +30%p 이상 우위
5. ✅ 재무 수치 답변의 정확도(Exact Match) 95% 이상
6. ✅ Faithfulness(환각률 역지표) 90% 이상
7. ✅ Azure OpenAI 의존 코드 0건
8. ✅ 외부 데이터는 모두 공개·합법 출처
9. ✅ Streamlit UI에서 질문 → 답변 + 출처 + 그래프 시각화 동시 표출
10. ✅ LLM 3종 비교 평가 리포트 산출
11. ✅ Planner 정확도 (LLM-as-judge) 85%+
12. ✅ Replan 발생률 20% 이하
13. ✅ 평균 노드 호출 수 < 8
14. ✅ 병렬 활성화 시 E2E 응답 < 8초 (순차 < 12초)

---

## 11. 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| DART API 호출 제한 (1만건/일) | 데이터 수집 지연 | 점진 수집 + 캐싱 + 우선순위 큐 |
| LLM 그래프 추출 노이즈 | 그래프 품질 저하 | 정형 데이터(지배구조·재무) 직접 매핑 우선, LLM 추출은 보조 |
| 한국어 LLM 비용 폭증 | 운영 부담 | 배치 작업은 로컬/저가 모델, 최종 합성만 고급 모델 |
| 시점 불일치 (분기/연도) | 답변 신뢰성 | 모든 데이터에 회계연도/분기 메타데이터 필수화 |
| 그래프 스키마 변경 부담 | 마이그레이션 비용 | 스키마 버전 관리 + 마이그레이션 스크립트 도입 |
| LLM Provider 장애 | 서비스 중단 | 어댑터 폴백 체인 (예: Claude 실패 시 GPT로 자동 전환) |

---

## 12. 향후 확장 가능성

본 시스템 완성 이후 다음 방향으로 확장 가능하다:

- **시계열 그래프(Temporal KG):** 시점별 지배구조 변화 추적
- **뉴스/공시 이벤트 노드:** 실시간 이벤트 반영
- **산업 분석 모드:** 산업 단위 비교·요약
- **포트폴리오 분석:** 사용자 보유 종목 기반 인사이트
- **다국어 확장:** 영문 글로벌 기업 데이터 (EDGAR 등)
- **자체 임베딩 파인튜닝:** 금융 도메인 특화 BGE-M3 fine-tuning

---

## 13. 부록: 핵심 의사결정 로그

| 결정 사항 | 선택 | 대안 | 사유 |
|---|---|---|---|
| 그래프 DB | Neo4j | Memgraph, ArangoDB | 생태계, Cypher 표준, GDS 플러그인 |
| 벡터 DB | Qdrant | Chroma, Weaviate, Milvus | 성능, 운영 안정성, 메타데이터 필터링 |
| 임베딩 | BGE-M3 | KURE, multilingual-e5 | 한국어 성능 + 멀티벡터 지원 |
| 정형 DB | PostgreSQL | MySQL, SQLite | JSONB, 확장성, 시계열 |
| 에이전트 프레임워크 | LangGraph | LangChain Agents, LlamaIndex | 명시적 상태 관리, 디버깅 용이 |
| LLM 추상화 | 자체 어댑터 | LiteLLM, LangChain LLM | 의존성 최소화, 도메인 제어 |
| UI | Streamlit | Gradio, Next.js | 빠른 프로토타이핑 |

---

**문서 끝.**

이 PRD를 기반으로 다음 단계는:
1. 팀 리뷰 및 합의
2. Phase 1 (인프라) 착수 — `docker-compose.yml` 및 LLM 어댑터 인터페이스 설계 확정
3. 각 Phase별 상세 기술 설계서 작성

추가로 보완하고 싶은 영역(예: 특정 데이터셋 상세 사양, 에이전트 라우팅 알고리즘 상세, 평가 QA 생성 방법론)이 있으면 별도 문서로 분리해서 작성 가능합니다.