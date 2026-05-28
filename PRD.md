# PRD: 자동차 제품·부품·리콜·공급망 GraphRAG 에이전트 시스템 v2.1

**문서 버전:** 2.1
**작성일:** 2026-05-27
**개정 사유:** v2.0 리뷰 피드백 반영 — (1) 포지셔닝 정밀화 ("제조" → "제품·부품·리콜·공급망"), (2) MVP 범위 현실화 (Level 6 → Level 3~4), (3) Bridge 일반화 (`corp_manufacturer` → `corp_entity`), (4) 관계 confidence/provenance 필수화, (5) Cross-Domain QA 4단계 층화

**v2.0 대비 주요 변경:**
- 제목·포지셔닝 변경 (§1.2)
- ER 마스터 키 구조 재설계 (§4.5 신설, §6.1 수정)
- Bridge 스키마 일반화 (§4.6 신설)
- BOM 깊이별 가용성 매트릭스 (§3.4 신설)
- 출처별 신뢰도 등급 (§3.5 신설)
- 관계 엣지 필수 메타데이터 정의 (§6.7 신설)
- Cross-Domain QA 4단계 층화 (§8.1 수정)
- MVP 수집 범위 축소 (§3.3 수정)

---

## 1. 프로젝트 개요

### 1.1 배경

AutoNexusGraph(금융 GraphRAG)는 다음을 입증했다:

- **3-Store 하이브리드**(Neo4j + PostgreSQL + pgvector)가 단일 Vector RAG로 풀 수 없는 멀티홉 질문을 해결
- **Multi-Agent + Planning(LangGraph)** 구조가 재현 가능·디버깅 가능한 추론을 만든다
- **Deterministic-first 추출**(정형 직매핑 + 선택적 LLM)이 환각을 원천 차단한다
- **LLM 어댑터 패턴**으로 벤더 종속 없이 운영 가능

그러나 AutoNexusGraph는 다음 한계를 가진다:

- **도메인 단일성:** 금융 한 영역에만 한정 — 시스템 일반성을 입증하지 못함
- **관계 평면성:** 자회사/임원/주주 관계가 모두 동일 평면. "메인 홉"과 "사이드 홉"의 구분이 없음
- **이벤트 빈도 낮음:** 공시·뉴스는 분기/월 단위
- **물리적 계층 부재:** 모든 엔티티가 법인. 제품·소재·공정 같은 물리적 계층이 없음

이 PRD는 AutoNexusGraph의 검증된 코어 엔진을 그대로 재사용하면서, **자동차 제품·부품·리콜·공급망 도메인**으로 도메인 어댑터만 교체하여:
1. 시스템의 도메인 일반성을 입증하고
2. 명시적 계층 구조(완성차 → 시스템 → 모듈 → 부품)를 통해 "메인 홉" 개념을 도입하며
3. AutoNexusGraph와 Bridge로 연결하여 **Cross-Domain 멀티홉 추론**이라는 GraphRAG-Only 가치 영역을 개척한다.

### 1.2 프로젝트 한 줄 정의 [v2.1 수정]

> **"자동차 제품·부품·리콜·공급망 공개 데이터를 기반으로, 완성차–시스템–모듈–부품의 계층 관계와 리콜·공급망 이벤트를 그래프로 추론하여 답변하는 GraphRAG 에이전트. 선택적으로 AutoNexusGraph와 Wikidata QID 기반 Bridge로 연결해 Cross-Domain 추론(제품/품질 ↔ 재무) 수행"**

**v2.0의 "자동차 제조 도메인"이라는 표현은 공정·라인·설비·원가·생산량을 기대하게 한다. 본 시스템의 실제 데이터 가용 범위는 공개 차량 제원·리콜·결함·NCAP·공급망이므로 "제품·부품·리콜·공급망"으로 포지셔닝한다.** Material/Process Level 6은 장기 확장 영역으로 분리.

### 1.3 핵심 변경사항 (AutoNexusGraph → AutoGraph)

| 영역 | AutoNexusGraph (AS-IS) | AutoGraph (TO-BE) | 변경 이유 |
|---|---|---|---|
| 인프라(Docker/Neo4j/PG/pgvector) | 그대로 | **그대로** | 코어 엔진 재사용 |
| LangGraph Multi-Agent | 그대로 | **그대로** | 노드 구조 동일, Tool만 교체 |
| Safety guards | 그대로 | **그대로** | 도메인 무관 |
| BGE-M3 임베딩 | 그대로 | **그대로** | 다국어 지원 |
| LLM 어댑터 | 그대로 | **그대로** | Provider 전환 환경변수 1줄 |
| **Entity Resolution 마스터** | **`corp_code` 단일 중심키** | **`entity_id` + `entity_type` 다형 키** | **법인·차량·부품 분리 [v2.1]** |
| **Bridge 테이블** | 없음 | **`bridge.corp_entity` (manufacturer + supplier 통합)** | **확장성 [v2.1]** |
| 데이터 소스 | DART/KRX/ECOS | NHTSA / car.go.kr / KATRI / Wikidata | 도메인 교체 |
| 핵심 엔티티 | Company / Person | **Manufacturer / Vehicle / Component / Supplier / Recall** | 도메인 교체 |
| 핵심 관계 | SUBSIDIARY_OF / EXECUTIVE_OF (평면) | PART_OF / SUPPLIED_BY / AFFECTED_BY (계층 + 시점) | 메인 홉 명시 |
| 정량 수치 | 재무제표 | 제원·NCAP·결함률 | 도메인 교체 |
| 이벤트 | 뉴스·공시 | 리콜·결함신고·NCAP 평가 | 도메인 교체 |
| **관계 엣지 메타** | snapshot_year + source | **confidence + provenance + valid_from/to 필수 [v2.1]** | **공급 관계 신뢰도 통제** |

---

## 2. 목적 및 목표

### 2.1 비즈니스 목적

1. **단일 도메인 GraphRAG의 한계를 넘는다**
   - 도메인 내: "현대 쏘나타의 에어백 리콜과 관련된 공급사는?" (멀티홉 + 시점)
   - Cross-Domain: "삼성SDI 배터리를 쓰는 OEM의 모회사 영업이익은?" (Vector RAG로 절대 불가)

2. **시스템의 도메인 일반성을 입증한다**
   - 동일 코어 엔진이 금융·자동차 양쪽에서 작동
   - 도메인 어댑터 레이어 교체만으로 새 도메인 진입 가능

3. **명시적 계층(메인 홉)으로 그래프 폭발을 통제한다**
   - 자연스러운 BOM 계층 (Manufacturer → Vehicle → System → Module → Part)
   - Planner가 계층 인지 깊이 우선 탐색 → 토큰·latency 절감

### 2.2 기술 목표 [v2.1 — Cross-Domain 층화 반영]

| 목표 | 측정 지표 | 목표치 |
|---|---|---|
| 한국어 자동차 RAG 정확도 | Answer Accuracy (LLM-as-judge) | 85%+ |
| Multi-hop 추론 성공률 (도메인 내) | 2-hop 이상 정답률 | 75%+ |
| **Cross-Domain L1 (제조사 ↔ 상장사 직접 Bridge)** | 정답률 | **80%+** |
| **Cross-Domain L2 (모델 ↔ 제조사 ↔ 재무)** | 정답률 | **70%+** |
| **Cross-Domain L3 (부품/공급사 ↔ OEM ↔ 재무)** | 정답률 | **50~60%** |
| **Cross-Domain L4 (시점 포함 공급망 ↔ 재무/ESG)** | 정답률 | **40~50%** |
| Hybrid 우위 입증 | Vector 단독 대비 Multi-hop 격차 | +30%p 이상 |
| 도메인 어댑터 교체 비용 | 코어 엔진 코드 변경량 | < 5% |
| 메인 홉 효율 | 평균 노드 탐색 수 (vs 평면 그래프) | 30% 감소 |
| 평균 응답 latency | 도메인 내 | < 8초 |
| Cross-Domain latency | Bridge join 포함 | < 12초 |
| 환각률 | Faithfulness (Ragas) | 90%+ |

**v2.0의 "Cross-Domain 60%+ 일률 목표"는 질문 난이도에 따라 너무 쉽거나 너무 어렵다. L1~L4 층화로 평가 신뢰도 확보.**

### 2.3 비목표 (Non-Goals) [v2.1 명시화]

- 차량 가격 예측 / 중고차 시세
- **공정·라인·설비·원가·생산량 데이터** ("제조"라는 표현이 기대하게 하나, 공개 데이터 없음)
- 비공개 OEM 내부 BOM
- 자율주행 안전성 인증 대체
- 정비 매뉴얼 기반 DIY 가이드
- 실시간 텔레매틱스
- **Level 6 (소재·공법) MVP 포함** — 장기 확장으로 분리

---

## 3. 데이터 정책

### 3.1 오픈 데이터 활용 원칙

AutoNexusGraph와 동일 원칙. 공공·라이선스 명시 데이터만 수집. 코드 레벨 라이선스 강제(`src/autograph/ingestion/_license.py`).

### 3.2 데이터 소스

#### 구축용 (Knowledge Source)

| 데이터 | 출처 | 라이선스 | 용도 |
|---|---|---|---|
| 차량 마스터 (제원·VIN 디코드) | NHTSA vPIC API | 공공(US) | `master.vehicles` |
| 리콜 (한국) | 자동차리콜센터 car.go.kr Open API | 공공 | `events.recalls` |
| 리콜 (글로벌) | NHTSA Recalls API | 공공 | `events.recalls` |
| 결함 신고 | NHTSA Complaints | 공공 | `vec.chunks` |
| 안전 평가 | KNCAP, NCAP, Euro NCAP | 공공 | `spec.measurements` |
| 자기인증·형식승인 | 국토부 KATRI | 공공 | `events.certifications` |
| 차량/제조사 글로벌 매핑 | Wikidata SPARQL | CC0 | `master.entity_map` + `wiki.wikidata_facts` |
| 차량/부품 위키 본문 | Wikipedia (ko/en) | CC BY-SA | `wiki.wikipedia_pages` + `vec.chunks` |
| 공급사 마스터 | KATECH, KAMA 공개자료 | 공공 | `master.suppliers` |
| 부품사 IR 자료 | 전자공시 + 공식 IR 사이트 | 공공 | `doc.manuals` + `vec.chunks` |

### 3.3 수집 범위 (MVP 1차) [v2.1 — 대폭 축소]

| 항목 | v2.0 (원안) | **v2.1 MVP** | 확장(post-MVP) |
|---|---|---|---|
| OEM | 20사 | **5~8사** (현대·기아·제네시스·KGM·르노코리아 + 토요타·BMW·테슬라) | 20사 |
| 모델 | 300종 | **30~50종** (대표 베스트셀러) | 300종 |
| 연식 | 2020~2024 | **2022~2024** | 2020~2024 |
| BOM 깊이 | Level 0~6 | **Level 0~4** | Level 5~6 |
| 리콜 | 한국+미국 5년 전수 | **NHTSA + 한국 주요 OEM 우선** | 5년 전수 |
| Cross-Domain QA | 30문항 | **10문항 seed → 30문항** | 50+ 문항 |
| Bridge 대상 | 30사 | **10~15사** (한국 OEM + 주요 부품사) | 30사+ |

**MVP는 5주 로드맵 내 실제 작동하는 시스템을 우선한다. 정합성 작업이 데이터 양에 묻히는 것을 방지.**

### 3.4 BOM 깊이별 데이터 가용성 매트릭스 [v2.1 신설]

| 계층 | 가용성 | MVP 포함 여부 | 권장 데이터 출처 |
|---|---|---|---|
| Level 0: Manufacturer | **높음** | ✅ 필수 | Wikidata + NHTSA + KAMA |
| Level 1: Vehicle Model | **높음** | ✅ 필수 | NHTSA vPIC + 리콜 + Wikipedia |
| Level 2: Trim/Year | **중간** | ✅ 필수 | NHTSA + 국내 매핑 수동 보강 |
| Level 3: System | **중간** | ✅ 포함 | KS/SAE 표준 분류 사전 + 리콜 분류 |
| Level 4: Module | **낮음~중간** | ⚠️ 부분 포함 (coverage 명시) | 공개 매뉴얼 + IR + 리콜 본문 LLM 추출 |
| Level 5: Part | **낮음** | ❌ MVP 제외 | 리콜/결함 중심으로만 진입 (post-MVP) |
| Level 6: Material/Process | **낮음** | ❌ MVP 제외 | 부품사 공개자료 / 일반 공법 지식 (장기) |

**MVP 성공 기준은 Level 0~4 안정 구축. Level 5는 리콜에 등장한 부품만 부분 포함. Level 6은 장기 로드맵.** 사용자에게도 UI에서 BOM 트리 표시 시 "Level 4까지 신뢰도 높음, 그 이하는 부분 데이터" 명시.

### 3.5 출처별 신뢰도 등급 [v2.1 신설]

PRD v2.0의 "출처 명시" 원칙을 정량화. 모든 그래프 엣지는 출처 등급에 따라 `confidence` 기본값이 결정된다.

| 출처 | 신뢰도 등급 | 기본 confidence | 적용 관계 |
|---|---|---|---|
| NHTSA / 자동차리콜센터 공식 리콜 | **A (높음)** | 0.95 | `AFFECTED_BY`, `RECALL_OF` |
| NHTSA vPIC | **A** | 0.95 | `MANUFACTURES`, `HAS_VARIANT` |
| KNCAP / NCAP / Euro NCAP | **A** | 0.95 | `SAFETY_RATED_BY` |
| Wikidata | **B (중간)** | 0.80 | 글로벌 ID 매핑, `MANUFACTURES` (보조) |
| Wikipedia | **B~C** | 0.70 | 설명 문서, 보조 근거 |
| 부품사 IR (공식 공시) | **B** | 0.75 | `SUPPLIED_BY` (후보) |
| 매뉴얼 / 브로셔 | **B** | 0.75 | `CONTAINS_*` (시스템·모듈) |
| LLM 추출 (P3) | **C** | 0.50 | P4 cross-validate 필수 |
| 커뮤니티 / 분해 자료 | **C (낮음)** | 0.40 | 후보 추출만, 확정 관계 금지 |
| 수동 검토 확정 | **A+** | 1.00 | 모든 관계 |

**`validated=true` 승급 정책:**
- `SUPPLIED_BY` 등 공급 관계는 **A 또는 B 출처 + P4 cross-validate 통과** 시에만 `validated=true`
- 그 외는 `candidate` 또는 `needs_review`
- C 등급 단독 출처는 절대 `validated=true` 금지

---

## 4. 시스템 아키텍처 방향성

### 4.1 컨테이너 토폴로지

AutoNexusGraph와 동일 인프라. 컨테이너는 그대로, 데이터만 다름.

```
[데이터 계층]
├─ Neo4j 5.18    : 차량·부품·공급사·리콜 그래프 (계층 + 시점 + confidence)
└─ PostgreSQL 16 : 제원 수치 / 차량·법인 마스터 / 평가 QA / 채팅 히스토리 /
                   LangGraph checkpoint / 문서 청크 벡터(pgvector) /
                   master.entities (다형 ER) / bridge.corp_entity

[모델 계층]
├─ BGE-M3        : 임베딩 (GPU) — AutoNexusGraph와 공유
└─ BGE-Reranker  : 재랭킹 (GPU) — 공유

[애플리케이션 계층]
├─ Ingestion Worker : NHTSA / car.go.kr / KATRI / Wikidata / Wikipedia / IR
├─ API (FastAPI)    : 에이전트 + 도메인 모드 라우팅
└─ Web (Streamlit)  : 도메인 토글 UI

[Bridge 계층]
└─ bridge.corp_entity : Wikidata QID + LEI + 사업자등록번호 기반 다형 join

[외부 의존성]
└─ LLM Provider : OpenAI / Anthropic / 로컬
```

### 4.2 데이터 흐름

AutoNexusGraph와 동일 5단계 + Bridge:
1. 수집 → PG 정형
2. 청킹 → pgvector
3. 그래프 구축 (계층 + confidence + provenance)
4. **Bridge: `master.entities.wikidata_qid` ↔ AutoNexusGraph `master.entity_map.wikidata_qid` 자동 매칭**
5. 질의 → 에이전트 → 답변
6. 평가 → 대시보드

### 4.3 그래프 vs 정형 DB 역할 분담

AutoNexusGraph 원칙 그대로:
- **Neo4j (관계 + 시점 + confidence):** BOM 계층, 공급 관계, 리콜 영향 범위
- **PostgreSQL (수치 + 의미):** 제원·NCAP 수치 + 매뉴얼/리콜 본문 청크 + 벡터
- **`master.entities` (신규):** 다형 ER 마스터 — 법인·차량·부품·리콜 통합 식별

**핵심 원칙:** 제원 수치는 절대 LLM이 생성하지 않는다.

### 4.4 메인 홉 계층 [v2.1 수정 — Level 4까지 안정, Level 5~6 분리]

```
[Level 0] Manufacturer       예: 현대자동차, 토요타       ← MVP 안정
   │ MANUFACTURES (class='main_hop')
   ▼
[Level 1] Vehicle Model      예: 쏘나타 DN8, 캠리 XV70    ← MVP 안정
   │ HAS_VARIANT (class='main_hop')
   ▼
[Level 2] Trim/Year          예: 쏘나타 1.6T 2024         ← MVP 안정
   │ CONTAINS_SYSTEM (class='main_hop')
   ▼
[Level 3] System             예: 파워트레인, ADAS         ← MVP 포함
   │ CONTAINS_MODULE (class='main_hop')
   ▼
[Level 4] Module             예: 가솔린 엔진, 배터리팩    ← MVP 부분 (coverage 명시)
   │ CONTAINS_PART (class='main_hop')
   ▼
[Level 5] Part               예: 인젝터, BMS              ← Post-MVP (리콜 등장만)
   │ MADE_OF / USES_PROCESS
   ▼
[Level 6] Material + Process 예: 알루미늄 합금 + 다이캐스팅 ← 장기 (확장 영역)

[사이드 홉]
- SUPPLIED_BY → Supplier      (Level 3~5, class='side_hop')
- MANUFACTURED_AT → Plant     (Level 1~2)
- COMPLIES_WITH → Standard    (Level 1~5)
- AFFECTED_BY → Recall        (Level 1~5, 시점 필수)
- COMPETES_WITH → Vehicle     (Level 1)
```

### 4.5 Entity Resolution 마스터 재설계 [v2.1 신설]

v2.0의 `vehicle_id` 단일 중심은 자동차 도메인에 부적합. 법인·차량·부품·리콜은 서로 다른 식별 체계가 필요.

```sql
CREATE TABLE master.entities (
    entity_id        VARCHAR PRIMARY KEY,        -- 내부 통합 ID (UUID 또는 prefix+seq)
    entity_type      VARCHAR NOT NULL,           -- manufacturer | supplier | vehicle_model
                                                  -- | vehicle_variant | component | recall | standard | plant
    canonical_name   VARCHAR NOT NULL,
    canonical_name_en VARCHAR,
    -- 외부 식별자 (entity_type에 따라 일부만 채워짐)
    wikidata_qid     VARCHAR,
    lei              VARCHAR,                    -- 법인만
    corp_code        VARCHAR,                    -- 한국 상장사만 (AutoNexusGraph 연동 키)
    business_no      VARCHAR,                    -- 한국 법인만
    cik              VARCHAR,                    -- SEC 등록 법인만
    nhtsa_model_id   VARCHAR,                    -- 차량 모델만
    nhtsa_campaign_id VARCHAR,                   -- 리콜만
    car_go_kr_id     VARCHAR,                    -- 한국 리콜만
    -- 메타
    source_priority  INT,                        -- 1=primary, 2=alias, ...
    confidence_score NUMERIC,
    valid_from       DATE,
    valid_to         DATE,
    schema_version   VARCHAR,
    created_at       TIMESTAMP DEFAULT NOW(),
    updated_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_entities_type ON master.entities(entity_type);
CREATE INDEX idx_entities_qid ON master.entities(wikidata_qid) WHERE wikidata_qid IS NOT NULL;
CREATE INDEX idx_entities_corp ON master.entities(corp_code) WHERE corp_code IS NOT NULL;
CREATE INDEX idx_entities_lei ON master.entities(lei) WHERE lei IS NOT NULL;
```

**엔티티 타입별 Primary Key 매핑:**

| 엔티티 타입 | 권장 식별자 (entities 행에서 활용) |
|---|---|
| Manufacturer | `entity_id`, `wikidata_qid`, `lei`, `corp_code` |
| Vehicle Model | `entity_id`, `wikidata_qid`, `nhtsa_model_id` |
| Vehicle Variant (Trim/Year) | `entity_id` (내부 생성) |
| Component | `entity_id` (내부 생성) |
| Supplier | `entity_id`, `wikidata_qid`, `lei`, `corp_code` |
| Recall | `entity_id`, `nhtsa_campaign_id`, `car_go_kr_id` |

**AutoNexusGraph와의 자연스러운 연결:** `entities.corp_code`가 채워진 행이 곧 Bridge 대상.

### 4.6 Bridge 일반화: `corp_entity` [v2.1 신설]

v2.0의 `bridge.corp_manufacturer`는 완성차 OEM만 다룬다. 실제 Cross-Domain 가치는 배터리사·반도체사·타이어사·ADAS 공급사까지 확장될 때 발현.

```sql
CREATE TABLE bridge.corp_entity (
    bridge_id         BIGSERIAL PRIMARY KEY,
    corp_code         VARCHAR NOT NULL,         -- AutoNexusGraph 키
    entity_id         VARCHAR NOT NULL,         -- AutoGraph master.entities.entity_id
    entity_type       VARCHAR NOT NULL,         -- manufacturer | supplier
                                                 --   (sub: battery_supplier | component_supplier
                                                 --    | semiconductor_supplier | tire_supplier | adas_supplier)
    -- 매칭에 사용된 식별자들 (감사·재현용)
    wikidata_qid      VARCHAR,
    lei               VARCHAR,
    cik               VARCHAR,
    business_no       VARCHAR,
    -- 매칭 메타
    match_method      VARCHAR NOT NULL,         -- qid_exact | lei_exact | business_no_exact
                                                 --   | corp_code_exact | fuzzy_name | manual
    confidence_score  NUMERIC NOT NULL,         -- 0.0 ~ 1.0
    -- 시점
    valid_from        DATE,
    valid_to          DATE,
    -- 거버넌스
    source            VARCHAR,                  -- wikidata | gleif | manual | derived
    reviewed_status   VARCHAR DEFAULT 'auto',   -- auto | reviewed | rejected
    reviewed_by       VARCHAR,
    reviewed_at       TIMESTAMP,
    schema_version    VARCHAR,
    created_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE(corp_code, entity_id, valid_from)
);

CREATE INDEX idx_bridge_corp ON bridge.corp_entity(corp_code);
CREATE INDEX idx_bridge_entity ON bridge.corp_entity(entity_id);
CREATE INDEX idx_bridge_type ON bridge.corp_entity(entity_type);
```

**매칭 우선순위 (Confidence 산정):**
1. `wikidata_qid` exact match → 0.95
2. `lei` exact match → 0.93
3. `business_no` exact match → 0.90
4. `corp_code` direct (AutoNexusGraph entity_map → AutoGraph 직접) → 0.95
5. Fuzzy name match (한글·영문 normalize 후) → 0.60~0.75
6. Manual → 1.00

**Confidence < 0.7은 자동 `needs_review` 큐로.**

이렇게 하면 "한온시스템 부품을 쓰는 차종의 한온시스템 재무 리스크"도 자연스럽게 풀린다.

---

## 5. LLM 추상화 전략

AutoNexusGraph와 100% 동일. 같은 `LLMClient`, 같은 어댑터, 같은 환경변수.

---

## 6. 도메인 변경에 따른 코드 재구성

### 6.1 마이그레이션 1:1 매핑 [v2.1 수정 — entities 통합 반영]

| AutoNexusGraph 자산 | AutoGraph 매핑 | 변경 정도 |
|---|---|---|
| `master.companies` | `master.entities` (entity_type='manufacturer') | **통합 ER로 일반화** |
| `master.persons` | `master.entities` (entity_type='supplier') 또는 별도 `master.persons` 유지 | 도메인 선택 |
| `master.entity_map` | `master.entities` 안에 흡수 | **단일 테이블로 통합** |
| `fin.financials` | `spec.measurements` | 시계열 구조 동일 |
| `fin.filings` | `doc.manuals` | 메타 구조 동일 |
| `news.articles` | `events.recalls` + `events.complaints` | 시점·멘션 구조 동일 |
| `wiki.*` | `wiki.*` | **완전히 동일** |
| `vec.chunks` | `vec.chunks` | **완전히 동일** (메타에 entity_id) |
| Neo4j `Company` 노드 | `Manufacturer` + `Vehicle` + `VehicleVariant` + `Component` + `Supplier` + `Recall` | 라벨 다양화 |
| `SUBSIDIARY_OF` | `MANUFACTURES` / `CONTAINS_*` (계층 main_hop) | 메인 홉 등급 부여 |
| `EXECUTIVE_OF` | `SUPPLIED_BY` / `MANUFACTURED_AT` | 인적 → 공급망 |

### 6.2~6.5 v1/v2/web/공통 재구성

v2.0과 동일 (생략).

### 6.6 추출 전략: AutoNexusGraph 4-Pass + Bridge Pass

| Pass | 입력 | 방식 | 산출물 | LLM 비중 |
|---|---|---|---|---|
| **P1 (Det)** | NHTSA vPIC / KNCAP / NCAP | 직접 매핑 | `spec.measurements` | 0% |
| **P2 (Det)** | 자동차리콜센터 정형, OEM 공개 BOM | 직접 매핑 | Neo4j 계층 + AFFECTED_BY | 0% |
| **P3 (LLM)** | 매뉴얼·결함신고·IR 본문 | Schema-aware LLM 추출 | 관계 후보 (SUPPLIED_BY 등) | 100% |
| **P4 (Validate)** | P3 산출 + P1/P2 + 출처 등급 | confidence 산정 + cross-validate | validated 관계 (§3.5 정책) | 보조 |
| **P5 (Bridge)** | `entities.wikidata_qid` ↔ AutoNexusGraph | 직접 매핑 + fuzzy fallback | `bridge.corp_entity` | 0% |

### 6.7 관계 엣지 필수 메타데이터 [v2.1 신설]

모든 관계 엣지(특히 `SUPPLIED_BY`, `USES_PROCESS`, `MADE_OF`, `MANUFACTURED_AT`, `AFFECTED_BY`, `COMPLIES_WITH`)는 다음 속성을 **필수**로 가진다:

```cypher
CREATE (a)-[r:SUPPLIED_BY {
    // 출처 정보 (provenance)
    source_type:        'recall' | 'ir_disclosure' | 'manual' | 'wikidata'
                        | 'wikipedia' | 'llm_extraction' | 'manual_curation',
    source_id:          'NHTSA-25V-001' | 'DART-20240315-...' | 'chunk_id:...',
    source_url:         'https://...' (optional),
    -- 추출 방식
    extraction_method:  'deterministic' | 'llm' | 'wikidata' | 'manual',
    extractor_version:  'p2-v1' | 'p3-llm-v2' | ...,
    -- 신뢰도
    confidence_score:   0.0 ~ 1.0,
    validated_status:   'candidate' | 'validated' | 'rejected' | 'needs_review',
    -- 시점 (시간 그래프)
    snapshot_year:      2024,
    valid_from:         date('2024-01-01'),
    valid_to:           date('2024-12-31') | null,
    -- 거버넌스
    created_at:         datetime(),
    reviewed_by:        'user_id' | null
}]->(b)
```

**Validator Agent 강제 규칙:**
- `validated_status='candidate'` 엣지는 답변 인용 시 "후보 정보" 명시
- `validated_status='rejected'` 엣지는 쿼리 시 자동 제외
- `confidence_score < 0.5` 엣지는 단독 근거 금지 (다른 A/B 출처와 결합 필요)

---

## 7. 에이전트 동작 방향성

### 7.0~7.6

v2.0의 §7 구조 그대로 + 다음 두 가지 신규 반영:

1. **Validator의 confidence 게이트:** §6.7 규칙을 Validator 단계에서 강제. confidence < 0.5인 엣지가 답변 근거에 포함되면 자동 fail → Replan.
2. **Bridge Tool의 confidence 표시:** `bridge_corp_to_manufacturer()` 호출 시 반환에 `bridge_confidence` 포함. UI는 0.7 이상은 ✓, 0.7 미만은 ⚠ 아이콘으로 표시.

### 7.2 도구 추상화 [v2.1 — entities 기반 시그니처]

#### `tools/spec.py`
- `lookup_entity(query, entity_type=None, limit=10)` — 통합 식별 (manufacturer/vehicle/supplier)
- `get_vehicle_info(entity_id)` / `get_spec(entity_id, year, metric)`
- `get_safety_rating(entity_id, year, agency)`
- `compare_vehicles(entity_ids, year, metric)`

#### `tools/graph.py`
- `lookup_entity(query, entity_type=None)` — Wikidata QID 포함 반환
- `list_components(vehicle_entity_id, level=None, max_depth=4, min_confidence=0.7, snapshot_year=None)` — **min_confidence 신규**
- `get_suppliers_of_component(component_entity_id, snapshot_year=None, min_confidence=0.7)`
- `get_vehicles_using_supplier(supplier_entity_id, snapshot_year=None)` — Cross-Domain의 핵심 진입점
- `list_recalls_affecting(vehicle_entity_id, year_range=None)`
- `find_paths(start_entity_id, end_entity_id, max_hops=3, only_main_hop=False)`

#### `tools/retrieve.py`
- v2.0과 동일 (메타 키만 `entity_id`)

#### `tools/bridge.py` [v2.1 — corp_entity 기반]
- `bridge_corp_to_entity(corp_code, entity_type=None)` — corp_code → 가능한 모든 AutoGraph 엔티티
- `bridge_entity_to_corp(entity_id)` — entity_id → corp_code (있다면)
- `cross_query_supplier_chain(supplier_corp_code)` — 한 줄로 "이 회사가 공급하는 차종 + OEM + OEM 재무" 패키지

---

## 8. 평가 및 검증 전략

### 8.1 평가 데이터셋 구성 [v2.1 — 4단계 층화]

#### 도메인 내 QA (총 100문항)
- Level 1 (단순 사실, 30): 단일 차량·단일 제원
- Level 2 (2-hop, 40): 차량↔부품, 부품↔공급사
- Level 3 (3-hop+, 30): 차량↔모듈↔부품↔공급사, 리콜 영향 범위

#### Cross-Domain QA (총 30문항, 4단계 층화) [v2.1 신설]

| 난이도 | 정의 | 문항 수 | 목표 정답률 | 예시 |
|---|---|---:|---:|---|
| **CD-L1** | 제조사 ↔ 상장사 직접 Bridge | 10 | **80%+** | "현대차가 제조한 모델의 리콜 건수와 현대차 영업이익을 같이 보여줘" |
| **CD-L2** | 차량 모델 ↔ 제조사 ↔ 재무 | 8 | **70%+** | "쏘나타 DN8을 만드는 회사의 최근 3년 영업이익 추이는?" |
| **CD-L3** | 부품/공급사 ↔ OEM ↔ 재무 | 8 | **50~60%** | "LG에너지솔루션 배터리를 쓰는 차종을 가진 OEM의 최근 영업이익은?" |
| **CD-L4** | 시점 포함 공급망 ↔ 재무/ESG | 4 | **40~50%** | "2023년 한온시스템에 공급계약 갱신한 OEM 중 KCGS ESG 등급이 B+ 이상인 회사는?" |

**각 QA 메타데이터:**
```json
{
  "id": "CD-L3-001",
  "question": "...",
  "answer": "...",
  "required_stores": ["AutoGraph.Graph", "Bridge", "AutoNexusGraph.SQL"],
  "required_confidence_min": 0.7,
  "hop_count": 4,
  "main_hop_path": ["Supplier", "Vehicle", "Manufacturer", "Financials"],
  "side_hops": [],
  "source_citations": ["..."]
}
```

### 8.2 비교 실험 매트릭스 [v2.1 — 저장소 명시]

각 질문이 어느 저장소를 써야 풀리는지 명시하여 Hybrid 필요성을 정량 입증:

| 유형 | 예시 | 필요한 저장소 | 측정 시스템 |
|---|---|---|---|
| SQL-only | "2024 쏘나타 1.6T 출력은?" | PostgreSQL | 4종 |
| Vector-only | "NHTSA 불만에서 자주 언급된 증상은?" | pgvector | 4종 |
| Graph-only | "이 부품을 쓰는 차종은?" | Neo4j | 4종 |
| Graph + SQL | "리콜된 차종의 안전등급 평균은?" | Neo4j + PG | 4종 |
| Graph + Vector | "리콜 사유와 관련된 시스템 설명은?" | Neo4j + pgvector | 4종 |
| **Cross-Domain** | "공급사를 쓰는 OEM의 영업이익은?" | AutoGraph + Bridge + AutoNexusGraph | **Bridge 시스템만** |

| 시스템 | L1 | L2 | L3 | CD-L1 | CD-L2 | CD-L3 | CD-L4 |
|---|---|---|---|---|---|---|---|
| Vector RAG only | 측정 | 측정 | 측정 | ~0% | ~0% | ~0% | ~0% |
| Graph RAG only | 측정 | 측정 | 측정 | N/A | N/A | N/A | N/A |
| Hybrid Agent (AutoGraph 단독) | 측정 | 측정 | 측정 | N/A | N/A | N/A | N/A |
| **Hybrid + Bridge (Cross-Domain)** | 측정 | 측정 | 측정 | **80%+** | **70%+** | **50~60%** | **40~50%** |

### 8.3 평가 지표

AutoNexusGraph 6개 지표 + 신규:
- Cross-Domain Bridge Hit Rate
- Main-Hop Efficiency
- **Confidence-Weighted Accuracy [v2.1]** — 답변 근거 엣지의 confidence 가중 평균 정확도

### 8.4 LLM 비교 평가

AutoNexusGraph와 동일 (GPT-4o / Claude / 로컬 3종).

---

## 9. 단계별 로드맵 [v2.1 — MVP 우선]

### Phase A1: 인프라 공유 + 스키마 (1주차)
- AutoNexusGraph Docker에 `master.entities`, `bridge.corp_entity`, `spec.*`, `events.*`, `doc.*` 추가
- BGE-M3 / LLM 공유 검증
- `.env` 추가 변수

### Phase A2: 데이터 파이프라인 MVP (2~3주차)
- **NHTSA vPIC + Recalls + Complaints** (글로벌 우선, 안정적 API)
- **자동차리콜센터 car.go.kr Open API** (한국)
- KNCAP/NCAP (스크래핑 가능 공개 자료만)
- Wikidata SPARQL (5~8 OEM + 주요 부품사)
- Wikipedia (해당 모델 30~50종)

### Phase A3: 그래프 구축 (3~4주차)
- P1: 제원 정형 (Level 0~2 완성)
- P2: 리콜/인증 정형 + BOM 계층 (Level 3 시스템 분류 사전 구축, Level 4 부분)
- P3: 매뉴얼/IR LLM 추출 (Level 4 Module 보강) — `confidence` 0.5 기본
- P4: cross-validate + 출처 등급에 따른 `validated_status` 갱신 (§3.5)
- **P5: Bridge 자동 매칭 + Confidence 산정**

### Phase A4: RAG + 에이전트 (4주차)
- Tools 4종 구현 (`spec`, `graph`, `retrieve`, `bridge`)
- Domain Router (UI 토글)
- Validator에 confidence 게이트 추가
- Cypher 계층 템플릿

### Phase A5: UI + 평가 (5주차)
- Streamlit 도메인 토글 + BOM 트리 (Level 표시)
- Cross-Domain QA 10문항 seed → 30문항 확장
- 5종 시스템 × 3 LLM 평가 매트릭스
- Confidence-가중 정확도 측정

---

## 10. 성공 기준 (Definition of Done) [v2.1 수정]

1. ✅ AutoNexusGraph `docker compose up` 그대로 AutoGraph까지 기동
2. ✅ Streamlit UI 도메인 토글 3종 동작
3. ✅ LLM Provider 환경변수 전환
4. ✅ **MVP 범위 (OEM 5~8사 × 모델 30~50종 × 2022~2024 연식)** 데이터 3저장소 적재
5. ✅ **BOM Level 0~3 안정, Level 4 coverage ≥ 60%** (Level 5~6은 post-MVP)
6. ✅ `bridge.corp_entity` 자동 생성 — Wikidata QID + LEI 매칭 confidence ≥ 0.9 비율 80%+
7. ✅ AutoGraph 단독 QA에서 Hybrid가 Vector 단독 대비 Multi-hop +30%p
8. ✅ **Cross-Domain QA 4단계 층화 목표 모두 달성** (CD-L1 80%+ / CD-L2 70%+ / CD-L3 50%+ / CD-L4 40%+)
9. ✅ 제원 수치 Exact Match 95%+
10. ✅ Faithfulness 90%+
11. ✅ **모든 `SUPPLIED_BY` 엣지에 confidence + provenance + snapshot_year 100% 채움**
12. ✅ AutoNexusGraph 코어 코드 변경 < 5%
13. ✅ 메인 홉 효율: 평균 노드 탐색 수 30% 감소
14. ✅ 평균 latency: 도메인 내 < 8초, Cross-Domain < 12초

---

## 11. 리스크와 대응 [v2.1 확장]

| 리스크 | 영향 | 대응 |
|---|---|---|
| 공개 데이터로 Level 5~6 BOM 채우기 어려움 | 깊은 부품 그래프 희소 | **MVP에서 Level 5~6 제외**, UI에 "Level 4까지 신뢰" 명시, post-MVP 분리 |
| `vehicle_id` 단일 키로 부족 | 법인·차량·부품 식별 혼란 | **`master.entities` 다형 키 구조 채택 (§4.5)** |
| Bridge 매칭 정확도 | Cross-Domain 환각 | Wikidata QID + LEI + 사업자번호 3중, confidence 표시, < 0.7은 needs_review |
| LLM 환각 공급 관계 | 그래프 오염 | **§3.5 출처 등급 + §6.7 confidence 필수 + Validator 게이트** |
| 시점 모호성 | 공급 관계 정확도 저하 | `snapshot_year` + `valid_from/to` 필수, 미상 시 명시 |
| OEM 비공개 BOM | Level 4 이하 한계 | Wikipedia + IR + 리콜 본문 + coverage 명시 |
| "제조" 표현이 공정·원가 기대 | 사용자 실망 | **§1.2 포지셔닝 "제품·부품·리콜·공급망"으로 변경** |
| Cross-Domain 목표치 불일치 | 평가 신뢰도 저하 | **§8.1 4단계 층화로 난이도별 목표 분리** |
| AutoNexusGraph 스키마 변경 시 Bridge 깨짐 | Cross-Domain 장애 | `schema_version` 명시, 마이그레이션 스크립트 |
| MVP 일정 압박 | 5주에 너무 큼 | **§3.3 범위 대폭 축소 (OEM 5~8사, 모델 30~50종)** |

---

## 12. 향후 확장 가능성

- **Level 5~6 부품·소재·공법 확장** (장기): 부품사 공개자료 + 분해 자료 정제 파이프라인
- **시계열 BOM:** 모델 연식별 부품 변경 추적 (Bridge `valid_from/to` 활용)
- **공급망 위험 분석:** Bridge로 공급사 집중도 + AutoNexusGraph 재무·신용도 결합
- **세 번째 도메인:** 동일 패턴으로 의약품/전자제품 확장 — N-Domain Bridge로 일반화
- **ESG ↔ 제품 Bridge:** AutoNexusGraph KCGS ESG와 차량 친환경성 결합
- **리콜 전파 분석:** 동일 부품 사용 차종 자동 영향 평가

---

## 13. 부록: 핵심 의사결정 로그 [v2.1 추가 항목]

| 결정 사항 | 선택 | 대안 | 사유 |
|---|---|---|---|
| 포지셔닝 | "제품·부품·리콜·공급망" | "자동차 제조" | 공개 데이터 가용 범위와 일치 |
| ER 마스터 키 | `entity_id` + `entity_type` 다형 | `vehicle_id` 단일 | 법인·차량·부품 식별 체계가 본질적으로 다름 |
| Bridge 대상 | `corp_entity` (manufacturer + supplier) | `corp_manufacturer` (OEM만) | 부품사 Cross-Domain 가치 흡수 |
| BOM MVP 깊이 | Level 0~4 | Level 0~6 | 공개 데이터 가용성 정직 반영 |
| 출처 신뢰도 | A/B/C 등급 + confidence 수치 | "출처 명시"만 | 그래프 오염 정량 통제 |
| Cross-Domain 평가 | 4단계 층화 (L1~L4) | 일률 60%+ | 난이도별 가치 명확화 |
| 도메인 라우팅 | UI 명시적 토글 | LLM 자동 분류 | 오분류 차단 |
| Bridge 키 | Wikidata QID 1차 + LEI + 사업자번호 | QID 단일 | 매칭 실패 완충 |
| 그래프 계층 | 엣지 속성 (`class`, `level`) | 노드 라벨 다양화 | 쿼리 단순성 |
| 인프라 공유 | AutoNexusGraph와 동일 컨테이너 | 별도 스택 | 운영 단순성 |

---

**문서 끝.**

## 다음 단계

1. **`master.entities` 마이그레이션 스크립트 설계** — AutoNexusGraph `master.entity_map`의 기존 데이터를 entities 다형 구조로 무손실 이전
2. **Bridge 자동 매칭 알고리즘 상세** — QID/LEI/business_no/fuzzy 우선순위 + confidence 산정 공식
3. **Cross-Domain QA 10문항 seed 큐레이션** — CD-L1 4문항 + CD-L2 3문항 + CD-L3 2문항 + CD-L4 1문항
4. **출처 신뢰도 → confidence 매핑 코드** — `src/autograph/ingestion/_confidence.py`
5. **Validator confidence 게이트 프롬프트** — 답변 근거 엣지의 confidence 자동 점검 로직