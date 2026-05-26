# FinGraph

> **한국 상장사 공시·재무 데이터를 기반으로, 기업 간 복잡한 관계를 그래프로 추론하여 답변하는 금융 분석 GraphRAG 에이전트**

Vector 단독 RAG가 풀지 못하는 멀티홉 추론(자회사 구조, 임원 겸직, 산업 연계)을 Graph + 정형 SQL + Vector 의 하이브리드로 풀어내는 시스템. Azure 종속을 제거하고 LLM Provider(OpenAI / Anthropic / 로컬)를 환경변수로 교체 가능하게 설계.

상세 요구사항은 [PRD.md](./PRD.md) 참조.

> ⚠️ **현재 단계:** Phase 1(인프라) 착수 전 — 초기 scaffold. 코드·docker 구성은 아직 비어 있음.

---

## 1. 핵심 특징

- **금융 특화 도메인** — DART 공시 / KRX 마스터 / ECOS 거시지표 → 코스피200+코스닥100 대상
- **3-Store 하이브리드** — Neo4j(관계) + PostgreSQL(수치) + Qdrant(의미) 역할 분리
- **Multi-Agent + Planning (LangGraph)** — Triage / Planner / Supervisor / Workers / Validator / Synthesizer 역할 분리, 명시적 DAG 계획, 검증·재계획 루프 — 상세 [PRD §7.5](./PRD.md#75-multi-agent--planning-상세-설계-langgraph)
- **채팅형 UI + 대화 히스토리** — 단발 질의 X, thread 기반 multi-turn, "위에서 답한 회사 중…" 같은 후속 질문 자연스럽게 — 상세 [PRD §7.6](./PRD.md#76-web-ui-채팅형--대화-히스토리-multi-turn)
- **Deterministic-first 추출** — XBRL 재무·지배구조는 정형 직매핑 (0% LLM), 서술형 관계만 selective LLM — 상세 [PRD §6.5](./PRD.md#65-추출-전략-v1v2-혼합-deterministic-first--selective-llm)
- **LLM 어댑터 패턴** — `LLMClient` 단일 인터페이스, `LLM_PROVIDER` 한 줄로 백엔드 교체
- **한국어 자체 임베딩** — BGE-M3 + BGE-Reranker GPU 컨테이너
- **재현 가능한 스택** — `docker compose up` 한 줄로 전체 시스템 기동 (Phase 1 부분 구축)
- **정량 검증 가능** — Multi-hop 100문항 자체 평가셋 + Allganize 금융 벤치마크

---

## 2. 아키텍처

```
[데이터 계층]
├─ Neo4j         : 기업·인물·관계 그래프
├─ PostgreSQL    : 재무 수치, 마스터, 평가 QA, 메타데이터
└─ Qdrant        : 문서 청크 벡터

[모델 계층]
├─ BGE-M3        : 한국어 임베딩 (GPU)
└─ BGE-Reranker  : 한국어 재랭킹 (GPU)

[애플리케이션 계층]
├─ Ingestion Worker : DART/KRX/ECOS 수집·전처리·그래프 추출 배치
├─ API (FastAPI)    : 에이전트 오케스트레이션 (LangGraph)
└─ Web (Streamlit)  : 사용자 인터페이스

[외부 의존성]
└─ LLM Provider : OpenAI / Anthropic / 로컬 (환경변수 전환)
```

### 저장소 역할 분리 원칙

| 저장소 | 책임 | 예시 질의 |
|---|---|---|
| Neo4j | **관계 탐색** | "현대차 자회사 중 매출 1조 이상은?" |
| PostgreSQL | **정확한 수치** | "삼성전자 2023년 매출은?" |
| Qdrant | **의미·서술** | "삼성전자의 주요 사업 위험 요인은?" |

> 재무 수치는 절대 LLM이 생성하지 않는다 — 반드시 PostgreSQL 조회 결과만 사용.

---

## 3. 데이터 소스

모든 데이터는 공개·합법 출처만 사용 (무단 크롤링·약관 위반 금지).

| 데이터 | 출처 | 형태 | 용도 |
|---|---|---|---|
| 사업보고서·공시 | DART Open API | XML/PDF | 본문 임베딩 + 관계 추출 |
| 재무제표 (XBRL) | DART | 정형 | PostgreSQL 정량 노드 |
| 상장사 마스터 | KRX 정보데이터시스템 | CSV | 종목·업종 분류 |
| 거시지표 | 한국은행 ECOS API | 시계열 | 거시 컨텍스트 노드 |
| 기업 지배구조 | DART 지배구조보고서 | 정형 | 임원·자회사 관계 |

**수집 범위 (1차):** 코스피 200 + 코스닥 100 약 300개사, 최근 3개 회계연도.

---

## 4. 에이전트 라우팅 (목표)

사용자 질문 유형에 따라 자동으로 도구를 조합.

| 질문 유형 | 예시 | 호출 도구 |
|---|---|---|
| 단순 사실 | "삼성전자 2023년 매출은?" | `query_financials` (PG 직접) |
| 의미·서술 | "삼성전자 주요 사업 위험 요인은?" | `search_documents` (Vector + Reranker) |
| 관계·구조 | "현대차 자회사 중 매출 1조 이상은?" | `query_graph` + `query_financials` |
| 멀티홉 | "이재용이 임원인 회사들의 합산 영업이익은?" | `query_graph` + `query_financials` + `search_documents` |

### Tool 목록

- `search_documents(query, filters)` — 벡터 검색
- `query_graph(cypher_intent)` — 그래프 탐색 (스키마 인지 Cypher 생성)
- `query_financials(company, year, metric)` — 재무 정확값 조회
- `lookup_company(name_or_ticker)` — 회사 식별
- `get_subgraph(entity, depth)` — 시각화용 서브그래프

답변은 항상 **출처(문서ID/페이지/노드ID) + 회계연도** 를 명시. 불확실하면 "정보 부족"으로 응답.

---

## 5. 평가 전략

### 평가셋 구성
- 공개 벤치마크: Allganize RAG-Evaluation-Dataset-KO (금융)
- 자체 구축 Multi-hop QA 100문항
  - Level 1 (단순 사실, 30) / Level 2 (2-hop, 40) / Level 3 (3-hop+, 30)

### 비교 매트릭스
Vector only / Graph only / **Hybrid Agent** / SQL+Vector — 4종 × LLM 3종 = 12조합

### 목표 지표

| 지표 | 목표 |
|---|---|
| Answer Accuracy (LLM-as-judge) | 85%+ |
| Multi-hop 정답률 (2-hop+) | 75%+ |
| Hybrid vs Vector-only Multi-hop 격차 | +30%p |
| 재무 수치 Exact Match | 95%+ |
| Faithfulness (Ragas) | 90%+ |
| 평균 latency | < 8초 |

---

## 6. 로드맵

| Phase | 주차 | 산출물 |
|---|---|---|
| 1. 인프라 | 1주차 | Docker Compose 스택, Neo4j/PG/Qdrant 부트스트랩, BGE-M3 GPU 컨테이너, LLM 어댑터 3종 |
| 2. 데이터 파이프라인 | 2주차 | DART/KRX/ECOS 수집기, PG 스키마 적재, 청킹+임베딩+Qdrant, LLM 그래프 추출 |
| 3. RAG 파이프라인 | 3주차 | Vector RAG, Graph RAG(스키마 인지 Cypher), SQL 도구 |
| 4. 에이전트 + UI | 4주차 | LangGraph 라우팅, Streamlit UI, 그래프 시각화 |
| 5. 평가 + 튜닝 | 5주차 | 100문항 QA, 12조합 평가, 대시보드, 파라미터 튜닝 |

---

## 7. 기술 스택

| 영역 | 선택 | 사유 |
|---|---|---|
| 그래프 DB | Neo4j | 생태계, Cypher 표준, GDS |
| 벡터 DB | Qdrant | 성능, 메타데이터 필터링 |
| 정형 DB | PostgreSQL | JSONB, 시계열 |
| 임베딩 | BGE-M3 | 한국어 성능 + 멀티벡터 |
| 에이전트 | LangGraph | 명시적 상태 관리 |
| LLM 추상화 | 자체 어댑터 | 의존성 최소화 |
| UI | Streamlit | 빠른 프로토타이핑 |

---

## 8. 비목표 (Non-Goals)

- 실시간 주가 예측 / 매매 신호 생성
- 비상장사 데이터 (DART 미제공)
- 영문 글로벌 기업 (1차 범위 외)
- 투자 자문 (정보 제공 한정)

---

## 9. 문서

- [PRD.md](./PRD.md) — 전체 요구사항·아키텍처 정의
  - §6.5 Deterministic-first + Selective LLM 추출 전략
  - §7.5 Multi-Agent + Planning 상세 설계 (LangGraph)
  - §7.6 채팅형 UI + 대화 히스토리

---

## 10. Quickstart (현재)

```bash
# 0. .env 작성 (.env.example 복사 후 DART_API_KEY, ECOS_API_KEY 채움)
cp .env.example .env

# 1. 의존성 설치
make install

# 2. (선택) 컨테이너 스택 기동 — Neo4j + PostgreSQL + Qdrant
make up
# 외부 포트: Neo4j HTTP 17474 / Bolt 17687 / PG 15432 / Qdrant 16333

make health         # 모든 컴포넌트 ping

# 3. 데이터 수집 (4단계 또는 make ingest-all)
make ingest-corp       # DART 회사 코드 마스터 (~3,900 상장사)
make ingest-krx        # KRX KOSPI top200 + KOSDAQ top100 (시가총액)
make ingest-targets    # corp_code × stock_code 매칭 → ingest_targets.jsonl
make ingest-bulk       # 295사 × 3년 일괄 (≈ 2~5분, 이어받기 지원)
make ingest-ecos       # 거시지표 (ECOS_API_KEY 필요)

# 4. 적재 결과 확인
make inventory         # 수집 현황·누락 검증

# 5. PG 적재 (docker stack 가동 + 스키마 적용 후)
make load-companies    # master.companies (295 rows)
make load-filings      # fin.filings     (4,584 rows)
make load-financials   # fin.financials  (184,199 rows, ~수 분)
# 또는 한 번에
make load-all
```

크롤러는 **이어받기·실패추적·Ctrl+C 안전종료** 지원 — 중단 시 `make ingest-bulk` 재실행하면 이어서, `python scripts/ingest/bulk_dart.py --retry-failed` 로 실패분만 재시도.

로더는 모두 **idempotent** (`INSERT ... ON CONFLICT DO UPDATE`) — 여러 번 실행해도 안전.

docker 셋업이 막히면 [docs/operations/docker_setup.md](./docs/operations/docker_setup.md) 참조 (3가지 시나리오 가이드).

상세는 [data/README.md](./data/README.md) 참조. LangGraph 에이전트 본체 + docker compose 의 BGE-M3/Reranker 는 후속 PR.

---

## 11. 라이선스

내부 연구·개발 단계. 라이선스 미정.
