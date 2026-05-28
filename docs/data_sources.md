# AutoGraph — 데이터 소스 카탈로그

작성일: 2026-05-28 · 조사 방식: web search + 공식 문서 + 학술 논문 + 기존 코드 비교

본 문서는 AutoGraph 도메인 (자동차 제품·부품·리콜·공급망 GraphRAG) 의 **모든 후보 데이터 소스**를 정리한다. 각 소스는 (1) 키/인증 요구, (2) 코드 적용 상태, (3) 어떤 PRD 항목·테이블·관계를 채우는지, (4) 미수집·미구현 사유, (5) 라이선스를 명시한다.

---

## 0. 요약 — 현재 통합 상태

| Tier | 정의 | 소스 개수 |
|---|---|---|
| **S** | 코드 통합 완료, 키 불필요 | 5 |
| **A** | 코드만으로 추가 가능, 키 불필요 | 9 |
| **B** | 키 발급 필요, 무료 (data.go.kr 등) | 7 |
| **C** | 스크래핑 또는 PDF 파싱 필요 | 5 |
| **D** | 상용/협의 필요 (제외) | 3 |

**PRD 의 BOM Level 0~4 / 출처 등급 A·B·C** 기준 매트릭스는 §6 참조.

---

## 1. Tier S — 통합 완료 (키 불필요)

### S1. **NHTSA vPIC** (Vehicle Product Information Catalog)
- **URL**: `https://vpic.nhtsa.dot.gov/api/`
- **무엇**: 차량 제조사·모델·연식·트림 마스터 + Canadian Vehicle Specs (제원)
- **포맷**: JSON REST · 키 불필요 · User-Agent 권장
- **채우는 곳**: `auto.master_manufacturers`, `auto.master_vehicle_models`, `auto.master_vehicle_variants`, `auto.spec_measurements` (dim/weight)
- **PRD §3.5 등급**: **A** (0.95)
- **모듈**: `autograph.ingestion.nhtsa_vpic` + `loaders.load_auto_pg.load_vpic` + `loaders.load_auto_specs`
- **갱신**: NHTSA 가 모델년도 단위 갱신 (연 1회)
- **누락**: US 시장 한정 — 한국 전용 트림·국내명은 미포함 (Wikidata 보강 필요)

### S2. **NHTSA Recalls API**
- **URL**: `https://api.nhtsa.gov/recalls/recallsByVehicle?make=&model=&modelYear=`
- **무엇**: 차종별 NHTSA 리콜 캠페인
- **포맷**: JSON · 키 불필요
- **채우는 곳**: `auto.events_recalls`, Neo4j `(VehicleVariant)-[:AFFECTED_BY]->(:Recall)`
- **PRD §3.5 등급**: **A** (0.95)
- **모듈**: `autograph.ingestion.nhtsa_recalls` + `loaders.load_auto_pg.load_recalls`
- **누락**: US 시장만. 한국 리콜은 §B2 (data.go.kr).

### S3. **NHTSA Complaints API**
- **URL**: `https://api.nhtsa.gov/complaints/complaintsByVehicle?make=&model=&modelYear=`
- **무엇**: 결함 신고 (소비자 불만)
- **포맷**: JSON · 키 불필요
- **채우는 곳**: `auto.events_complaints`, `vec.chunks` (`source='nhtsa_complaint'`), Neo4j `:Complaint`
- **PRD §3.5 등급**: **A** (0.95)
- **모듈**: `autograph.ingestion.nhtsa_complaints` + `loaders.load_auto_pg.load_complaints` + `build_chunks_auto`

### S4. **NHTSA SafetyRatings API** (P0 추가 완료)
- **URL**: `https://api.nhtsa.gov/SafetyRatings/modelyear/{Y}/make/{M}/model/{Mod}`
- **무엇**: NCAP 5-star 전체·정면·측면·전복·폴 등급 + ESC/FCW/LDW 기능 유무
- **포맷**: JSON · 키 불필요
- **채우는 곳**: `auto.spec_measurements.safety.ncap.*`, Neo4j `(VehicleVariant)-[:SAFETY_RATED_BY]->(:Standard {code:'NCAP_US'})`
- **PRD §3.5 등급**: **A** (0.95)
- **모듈**: `autograph.ingestion.nhtsa_safety_ratings` + `loaders.load_auto_safety`
- **누락**: NHTSA NCAP 만 (US). KNCAP/EuroNCAP 별도 필요 (§C2, §C4).

### S5. **Wikidata SPARQL**
- **URL**: `https://query.wikidata.org/sparql`
- **무엇**: 제조사 / 모델 / 공급사 마스터 + QID (글로벌 ID) + LEI (P1278) + 한국 사업자번호 (P3320) + 부품→공급사 P176
- **포맷**: SPARQL · 키 불필요 · User-Agent 필수
- **채우는 곳**: `auto.master_manufacturers/wikidata_qid`, `bridge.corp_entity`, `auto.master_suppliers`, `auto.staging_relations` (SUPPLIED_BY)
- **PRD §3.5 등급**: **B** (0.80)
- **모듈**: `autograph.ingestion.wikidata_auto` + `loaders.load_bridge` + `loaders.load_wikidata_part_supplies` (P4 완료)
- **누락**: Wikidata 자동차 부품 P176 sparse — 큰 OEM 의 메이저 부품 외엔 거의 없음. LLM P3 가 보완.

### S6. **Wikipedia (ko/en) REST API** (P3 추가 완료)
- **URL**: `https://{lang}.wikipedia.org/w/api.php?action=query&prop=extracts|info|pageprops` + `action=parse`
- **무엇**: 자동차 모델/제조사 본문 + Infobox 구조 데이터
- **포맷**: JSON · 키 불필요 · CC BY-SA 4.0
- **채우는 곳**: `vec.chunks` (`source='wikipedia_auto'`), narrative QA 검색
- **PRD §3.5 등급**: **B~C** (0.70)
- **모듈**: `autograph.ingestion.wikipedia_auto` + `loaders.build_chunks_auto.build_from_wikipedia`
- **누락**: 한국어판은 모델 detail 적음 → 영어판 fallback + (옵션) 나무위키 보강 (§C5).

### S7. **AI Hub 자동차 라벨링 데이터** (다운로드 형식)
- **URL**: `https://aihub.or.kr` (다운로드: `bin/aihubshell`)
- **데이터셋**: 71347 (자율주행 고장진단), 578 (부품 품질 검사 영상)
- **무엇**: 모터-감속기 / 배터리 / 도어 / 범퍼 등 부품×결함 라벨
- **포맷**: TL/VL JSON in zip/tar · **AI Hub API 키 필요** (회원가입 무료)
- **채우는 곳**: `auto.components` (Module), `vec.chunks` (`source='aihub_71347|578'`), Neo4j `:Module + CONTAINS_COMPONENT`
- **모듈**: `autograph.ingestion.aihub` + `loaders.load_auto_aihub`
- **누락**: Tier S 분류로 두지만, 키 발급은 사용자 회원가입 필요. 다운로드 승인 별도.

---

## 2. Tier A — 키 불필요, 코드 추가만 필요

### A1. **NHTSA Investigations API** (별도 endpoint 확인 필요)
- **URL 후보**: `data.transportation.gov/Automobiles/...` 의 Socrata SODA + `crashviewer.nhtsa.dot.gov/CrashAPI`
- **무엇**: 리콜 전단계 **결함 조사** history (NHTSA ODI 가 개시·종료한 조사) — recall 보다 깊은 결함 패턴
- **포맷**: SODA REST (Socrata) · 키 불필요 (rate-limit 있음, app token 권장)
- **채우는 곳**: `auto.events_investigations` (신규 테이블 필요) 또는 events_recalls 확장
- **PRD §3.5 등급**: **A** (0.95)
- **작업량**: ~120 LOC (recalls 패턴 복제)
- **누락 보강**: 진행 중 조사 → 향후 리콜 예측 신호

### A2. **NHTSA Technical Service Bulletins (TSB) — Socrata 다운로드**
- **URL**: `https://catalog.data.gov/dataset/nhtsas-office-of-defects-investigation-odi-technical-service-bulletins-system-tsbs-downloa`
- **무엇**: OEM 가 NHTSA 에 제출한 TSB (서비스 통신문) — 결함 패턴·수리 가이드
- **포맷**: ZIP CSV (`FLAT_TSBS.zip`) · 키 불필요
- **갱신**: 일 단위
- **채우는 곳**: `vec.chunks` (신규 source='nhtsa_tsb') — narrative 검색
- **PRD §3.5 등급**: **A** (0.90)
- **작업량**: ~100 LOC (CSV downloader + chunker)

### A3. **NHTSA FARS / Crash data (FTP + Crash API)**
- **URL**: `https://crashviewer.nhtsa.dot.gov/CrashAPI` + FTP CSV download
- **무엇**: 미국 치명사고 데이터 (1975~현재) — 차종별 안전성 사후 신호
- **포맷**: CSV/SAS · 키 불필요
- **채우는 곳**: 신규 `auto.events_crashes` 또는 `spec_measurements.safety.fars_*`
- **PRD §3.5 등급**: **A** (0.95)
- **누락 보강**: 충돌 통계 — recall 빈도가 적은 차종에도 신호 제공
- **작업량**: ~150 LOC

### A4. **EPA fueleconomy.gov 데이터**
- **URL**: `https://www.fueleconomy.gov/feg/download.shtml` (CSV/zip) + `https://www.fueleconomy.gov/feg/ws/rest/vehicle/{id}`
- **파일**: `vehicles.csv.zip` (1984~현재) + `emissions.csv.zip`
- **무엇**: US 차량 MPG (city/highway/combined), 엔진·변속기 spec, 배출가스 등급, GHG score, SmartWay
- **포맷**: CSV/XML · 키 불필요
- **채우는 곳**: `auto.spec_measurements.spec.efficiency.*`, `spec.emissions.*`, `spec.engine.*`
- **PRD §3.5 등급**: **A** (0.95)
- **작업량**: ~150 LOC (CSV downloader + variant 매칭)
- **누락 보강**: PRD §10.9 "제원 수치 EM 95%+" 직접 기여

### A5. **EPA Annual Certification Data**
- **URL**: `https://www.epa.gov/compliance-and-fuel-economy-data/annual-certification-data-vehicles-engines-and-equipment`
- **무엇**: 차량/엔진 제조사 인증 자료 — Tier 3 emissions, Federal/CARB 인증
- **포맷**: XLSX (CSV 변환 필요) · 키 불필요
- **채우는 곳**: `auto.spec_measurements.spec.emissions.tier3_*`, Standard 노드 enrichment
- **PRD §3.5 등급**: **A** (0.95)
- **작업량**: ~120 LOC

### A6. **DBpedia SPARQL**
- **URL**: `http://dbpedia.org/sparql`
- **무엇**: Wikipedia 추출 구조 데이터 — `dbo:Automobile`, `dbo:manufacturer`, `dbo:parentCompany`, `dbp:assembly`, `productionStartYear` 등
- **포맷**: SPARQL · 키 불필요 · User-Agent 권장
- **채우는 곳**: `auto.master_*` wikidata_qid 부족분, Neo4j Manufacturer parent 관계
- **PRD §3.5 등급**: **B** (0.80) — Wikipedia 파생
- **작업량**: ~120 LOC (wikidata_auto 패턴 복제)
- **누락 보강**: Wikidata 가 부족한 textual properties (model 설명·생산국·플랫폼 코드)

### A7. **SEC EDGAR Company Facts API** (글로벌 OEM)
- **URL**: `https://data.sec.gov/api/xbrl/companyfacts/CIK{0-padded-10digit}.json`
- **무엇**: Tesla / Ford / GM / Toyota ADR / Honda ADR / 등 글로벌 상장 OEM 의 XBRL 정제 데이터 (매출·생산·리콜 charge·R&D 등)
- **포맷**: JSON · 키 불필요 · User-Agent 필수 (`"App Name email@..."`)
- **Rate**: 10 req/s SEC 전체
- **채우는 곳**: `master.financial_*` (finance), `bridge.corp_entity` 강화 — cross_domain QA 의 핵심
- **PRD §3.5 등급**: **A** (0.95)
- **작업량**: ~80 LOC (finance `sec_client.py` 가 이미 있어 OEM CIK 리스트만 추가)
- **누락 보강**: 한국 OEM 은 KOSDAQ/KOSPI → DART 측 finance 모듈이 처리. 글로벌 OEM 만 SEC.

### A8. **Open Charge Map API** (EV)
- **URL**: `https://api.openchargemap.io/v3/poi/` (키 옵션, 무키 시 sample)
- **무엇**: 전세계 EV 충전소 위치·전력·운영자
- **포맷**: JSON/XML · 무키도 호출 가능 (live 데이터는 키 권장)
- **채우는 곳**: 신규 `auto.charging_stations`, EV 모델 컨텍스트 (subgraph)
- **PRD §3.5 등급**: **B** (0.80)
- **작업량**: ~100 LOC
- **누락 보강**: 전기차 모델의 인프라 신호 (현지 보급 추세)

### A9. **automotive-ontology.org / AUTO (edmcouncil/auto)** — Schema 보강
- **URL**: `https://github.com/edmcouncil/auto`
- **무엇**: W3C Automotive Ontology Community Group + EDM Council 의 OWL 온톨로지 (FIBO 패턴) — class/property SSOT
- **라이선스**: Apache 2.0
- **채우는 곳**: `ontology/auto/entities.yaml` 검증 — 외부 표준과 정렬
- **작업량**: ~40 LOC (yaml 비교·차이 보고)
- **누락 보강**: 우리 ontology 가 표준 따르는지 자동 검증

---

## 3. Tier B — 키 발급 필요, 무료

### B1. **공공데이터포털 (data.go.kr) — 국토교통부 자동차 리콜정보 API**
- **URL**: `https://www.data.go.kr/data/15089863/openapi.do`
- **무엇**: **국내 출시 승용차 리콜 + 무상수리** — NHTSA 가 못 보는 한국 시장
- **포맷**: REST · **인증키 필요 (포털 회원가입 후 즉시 무료 발급)**
- **채우는 곳**: `auto.events_recalls` (source='molit_kr'), Neo4j AFFECTED_BY
- **PRD §3.5 등급**: **A** (0.95)
- **작업량**: ~120 LOC (nhtsa_recalls 패턴)
- **누락**: 현재 `car_go_kr_recalls.py` 는 manual CSV 모드만 — 키 발급 시 API 호출로 전환

### B2. **공공데이터포털 — 국토교통부 자동차종합정보 API** (`15071233`)
- **URL**: `https://www.data.go.kr/data/15071233/openapi.do`
- **무엇**: 차량 기본정보 / 제원정보 / 이력정보 / 성능점검 — VIN 또는 차량등록번호 기반
- **포맷**: REST · **인증키 + 별도 승인 필요** (car365.go.kr 신청)
- **채우는 곳**: 차량 단위 spec 보강 (개인 차량 단위, 모집단 통계 아님)
- **누락**: 개별 차량 조회용 — 마스터 데이터 보강에는 부적합 (별도 fleet 확보 필요)

### B3. **공공데이터포털 — 한국교통안전공단 자동차종합정보 신규등록정보** (`15059401`)
- **URL**: `https://www.data.go.kr/data/15059401/openapi.do`
- **무엇**: 등록년·등록월·차종코드·지역코드별 신규등록 통계
- **포맷**: REST · 인증키 (무료)
- **채우는 곳**: 신규 `auto.market_registrations` (시계열 통계) — 시장 점유율 분석
- **PRD §3.5 등급**: **A** (0.95)

### B4. **KOSIS 공유서비스 (통계청)**
- **URL**: `https://kosis.kr/openapi/`
- **무엇**: 자동차 등록대수 (672건 관련 통계), 생산·수출입 시계열
- **포맷**: REST · 키 (개발계정 1000 트래픽/일 무료)
- **채우는 곳**: `master.macro_*` (finance 측에 이미 패턴) — 거시 컨텍스트
- **누락**: finance 측 `kosis_client.py` 가 이미 있음. 자동차 통계 ID 만 추가하면 됨.

### B5. **공공데이터포털 — KAMA 자동차 생산량** (`15051116`)
- **URL**: `https://www.data.go.kr/data/15051116/fileData.do`
- **무엇**: 국내 및 세계 자동차 생산량 통계 (산업통상자원부 / KAMA)
- **포맷**: 파일 다운로드 (CSV/Excel) · 로그인 무필요
- **채우는 곳**: `auto.market_production` (제조사·국가·연도 시계열)

### B6. **car365.go.kr 자동차민원 포털**
- **URL**: `https://www.car365.go.kr/`
- **무엇**: 자동차종합정보 파일자료 (배치성) — JSON/CSV bulk
- **포맷**: 다운로드 · 데이터프리존 예약 필요
- **누락**: 사용자가 KOTSA 담당자 (054-459-7264) 에 신청 절차 거쳐야 함

### B7. **국토교통부 통계누리 — 자동차등록현황**
- **URL**: `https://stat.molit.go.kr/portal/cate/statMetaView.do?hRsId=58`
- **무엇**: 월별 자동차 등록 통계 (전국·시도·차종)
- **포맷**: Excel 다운로드 · 무키
- **작업량**: 30 LOC (월별 URL 패턴 + Excel parser)

---

## 4. Tier C — 스크래핑·PDF 파싱·라이선스 주의

### C1. **자동차리콜센터 (car.go.kr) 공식 사이트** (CSV 수동 다운로드)
- **URL**: `https://www.car.go.kr/home/main.do`
- **무엇**: §B1 의 backup — API 미공개분은 web 화면에서 검색·다운로드만 가능
- **이미 코드 있음**: `autograph.ingestion.car_go_kr_recalls` 가 `data/raw/auto/car_go_kr/*.csv` 정규화 지원
- **누락**: 자동화 미적용 — 사용자가 정기적 CSV 다운로드 필요

### C2. **EuroNCAP 결과 페이지** (HTML 스크래핑)
- **URL**: `https://www.euroncap.com/en/results/`
- **무엇**: 유럽 차량 안전 등급 — 정면·측면·아이·보행자·SA(Safety Assist) 별점
- **포맷**: HTML · robots.txt 허용 · 스크래핑 가능 (rate-limit 보수)
- **채우는 곳**: `auto.spec_measurements.safety.euroncap.*`, Neo4j SAFETY_RATED_BY (Standard='EURO_NCAP')
- **PRD §3.5 등급**: **A** (0.95) — 공식 기관
- **작업량**: ~150 LOC (BeautifulSoup + 페이지 구조 변경 대응)
- **대안 API**: `regcheck.org.uk/api/bespokeapi.asmx` SOAP — 회원가입 무료 무비용 (UK)

### C3. **KIDI (보험개발원) 자동차 등급요율 PDF**
- **URL**: `https://www.kidi.or.kr/` 등급요율공시
- **무엇**: 자동차 사고율·수리비·도난율 → 차종별 보험요율 (결함 사후 신호)
- **포맷**: PDF 분기별 · 무키
- **작업량**: ~200 LOC (pdfplumber 등 PDF 표 추출)

### C4. **KNCAP (한국 신차 안전도 평가)** — 자동차안전연구원
- **URL**: `https://www.kncap.org/` — 평가결과 PDF
- **무엇**: 한국 차량 안전 등급 — EuroNCAP/NHTSA 외 국내 평가
- **포맷**: PDF + 별점 표
- **작업량**: ~120 LOC (PDF 파싱)

### C5. **나무위키 자동차 페이지** (라이선스 NC 주의)
- **URL**: `https://namu.wiki/w/{차종_또는_제조사}`
- **무엇**: 한국어 차량 detail — Wikipedia ko 보다 풍부 (특히 한국 모델)
- **포맷**: HTML or DB dump (Internet Archive 에서 ~월 1회 dump)
- **라이선스**: **CC BY-NC-SA 2.0 KR** — **비상업 한정**
- **갱신**: dump 비정기, archive.org/details/namuwikidumps
- **대안 도구**: `lovit/namuwikitext` (Korpora 데이터셋, 4.7 GB, 2020-10-25 마지막)
- **누락**: NC 라이선스 → 상업 서비스 시 사용 금지. 연구·내부용 OK.

---

## 5. Tier D — 상용 / 제외

### D1. **Marklines (marklines.com)**
- **상태**: 상용 paid · 학술 연구용 정식 협의 가능 (academic license)
- **카테고리**: company / customer / country / certificate / product 5-entity 글로벌 supply network
- **사용 사례**: 학술 논문 다수 (예: 2107.10609, 2305.08506) — 5-layer 다층 그래프 supply chain
- **결정**: 라이선스 비용으로 제외. supplychain-dataset-gen + Wikidata P176 + 자체 LLM P3 추출로 대체.

### D2. **JATO Dynamics**
- **상태**: 상용 paid · 시장 점유율·가격 detail
- **결정**: 제외

### D3. **Edmunds / CarAndDriver / Motor1**
- **상태**: ToS 가 스크래핑 명시 금지
- **결정**: 제외

---

## 6. PRD BOM Level × 데이터 가용성 매트릭스 (재정리)

| Level | 정의 | 가용 소스 (활용도 순) | 현재 채워짐? |
|---|---|---|---|
| **L0 Manufacturer** | 제조사 | Wikidata, NHTSA vPIC MakeId | ✅ |
| **L1 VehicleModel** | 모델 | NHTSA vPIC, Wikidata, Wikipedia, DBpedia | ✅ |
| **L2 Trim/Year (Variant)** | 트림·연식 | NHTSA vPIC GetModelsForMakeYear, Canadian Specs | ✅ |
| **L3 System** | 시스템 (powertrain, brake, body…) | ontology system_taxonomy.yaml (SSOT) | ✅ (derived `CONTAINS_SYSTEM` 완료) |
| **L4 Module** | 모듈 (Motor-Reducer, Battery Pack, Door…) | AI-Hub 71347/578, Wikidata P176 후보, LLM P3 | ⚠️ 부분 — AI-Hub 외 sparse |
| **L5 Part** | 부품 (셀·센서·인플레이터) | 리콜 본문 LLM 추출만 (P3 RECALL_OF) | ❌ 매우 sparse |
| **L6 Material/Process** | 소재·공법 | PRD 명시적 non-goal | ❌ post-MVP |

---

## 7. PRD 출처 등급 × 현재 구현 매트릭스

| 등급 | confidence | 적용 소스 | 통합 상태 |
|---|---|---|---|
| **A+** 0.95+ (verified) | 수동 검토 | 매뉴얼 seed, supplier_seed.yaml | ✅ |
| **A** 0.95 | NHTSA recalls/vPIC/NCAP, KNCAP, EuroNCAP, KAMA, DART | ✅ NHTSA 만. KNCAP/EuroNCAP 미통합 |
| **B** 0.80 | Wikidata, EPA, 매뉴얼/브로셔 | ✅ Wikidata. EPA 미통합 (§A4, A5) |
| **B~C** 0.70 | Wikipedia, DBpedia | ✅ Wikipedia (P3 완료). DBpedia 미통합 (§A6) |
| **C** 0.50 | LLM P3 추출 | ✅ P3 staging + cross_validate |
| **C-** 0.40 | 커뮤니티 / 비공식 | ❌ 사용 안 함 (PRD 정책) |

---

## 8. 관련 학술 논문 (조사 결과)

| 논문 | 핵심 기여 | 본 프로젝트와 관계 |
|---|---|---|
| **arXiv 2411.19539** — *Knowledge Management for Automobile Failure Analysis Using Graph RAG* (IEEE BigData 2024) | OEM 결함 분석에 GraphRAG 적용. ROUGE F1 +157.6% 개선 보고. 자체 Q&A 데이터셋. 코드 비공개. | **직접 유사** — failure analysis 가 우리 vehicle_recall 분기와 같은 use case |
| **arXiv 2504.01248** — *Automated Factual Benchmarking for In-Car Conversational Systems using LLMs* | 차량 대화형 시스템의 factual benchmarking 자동화. | gold QA 생성 자동화 참고 |
| **arXiv 2012.02558** — *Pre-trained language models as knowledge bases for Automotive Complaint Analysis* | NHTSA ODI complaints 로 PLM 도메인 적응. | 우리 P3 추출의 baseline |
| **arXiv 2107.10609** — *Data Considerations in Graph Representation Learning for Supply Chain Networks* | Marklines 데이터로 글로벌 자동차 공급망 그래프 representation learning. SOTA on link prediction. | 우리 SUPPLIED_BY 평가의 reference |
| **arXiv 2305.08506** — *A Knowledge Graph Perspective on Supply Chain Resilience* | KG 기반 공급망 회복력 분석 framework. | bridge.corp_entity 확장 방향 |
| **MDPI Electronics 2025** — *Document GraphRAG for Manufacturing* | 제조 도메인 GraphRAG. KG + RAG 결합으로 retrieval robustness. | Hybrid adapter 의 baseline |
| **arXiv 2409.20010** — *Customized Domain-centric KG Construction with LLMs* (자동차 전기 시스템) | 자동차 전기 시스템 도메인 KG 자동 구축. GraphGPT/REBEL 대비 우수. | 우리 LLM P3 추출의 직접 reference |
| **ACM AIAA 2024** — *NER of New Energy Vehicle Parts via LLM* | LFRC (LLM+Fine-tune+Reflective CoT) — 신에너지차 부품 NER | EV 부품 추출 strategy |
| **PMC 2024** — *Chinese NER for Automobile Fault Texts* | external context retrieving + adversarial training | recall text 정규화 patterns |

---

## 9. Open Datasets (재사용 가능)

### 9.1 봉인된 그래프 데이터셋

| 데이터셋 | 라이선스 | 규모 | 형태 | 활용 |
|---|---|---|---|---|
| **wey-gu/supplychain-dataset-gen** | Apache 2.0 | 40 vertices, 62 edges (sample) | NebulaGraph CSV | 우리 SUPPLIED_BY seed schema 검증 |
| **edmcouncil/auto** OWL ontology | Apache 2.0 | ~수백 클래스 | OWL/RDF | `ontology/auto/*.yaml` 검증 |

### 9.2 코퍼스 / NLP

| 데이터셋 | 라이선스 | 규모 | 활용 |
|---|---|---|---|
| **lovit/namuwikitext** | CC BY-NC-SA 2.0 KR | 4.7 GB / 31.2M lines | 한국어 자동차 narrative 청크 (비상업 한정) |
| **Internet Archive 나무위키 dumps** | CC BY-NC-SA 2.0 KR | 정기 dump | 위 + raw HTML |
| **Salesforce/wikitext** (Hugging Face) | CC BY-SA 3.0 | 영어 일반 | 자동차 fine-tune 부적합 (general) |
| **Kaggle: nhtsa/safety-recalls** | NHTSA public | 1967~현재 | 우리 NHTSA recalls API 와 중복 — skip |

---

## 10. 데이터 GAP 분석 (PRD §3.4 기준)

### 🟢 충분 (현재 인프라로 채워짐)
- **L0 Manufacturer**: NHTSA + Wikidata 가 글로벌 커버. KAMA 가 한국 보강.
- **L1 VehicleModel**: NHTSA vPIC + Wikipedia. ko/en 양면.
- **L2 Variant**: NHTSA vPIC + Canadian Specs. US 한정이지만 한국 OEM 의 글로벌 변형 다수 커버.
- **L3 System**: ontology SSOT + derived CONTAINS_SYSTEM. 자체 분류 안정.

### 🟡 부분 부족
- **L4 Module**: AI-Hub 71347/578 만 deterministic. 일부 카테고리 (Motor-Reducer / Battery / Door / Bumper …) 만. 나머지는 LLM P3 추출 의존 → confidence 0.50~0.80 가 다수.
  - **Gap 해소**: Wikidata P176 자동 추출 확장 (§S5 staging 완료) + DBpedia P527 (§A6) + EPA 인증 데이터 (§A5) 보완.
- **시계열 / 시점 메타**: PRD §6.7 의 `snapshot_year` 가 NHTSA recalls 에는 잘 채워지지만 manufacturer/model 마스터에는 sparse.
  - **Gap 해소**: KOSIS 신규등록 통계 (§B4) + 국토부 통계누리 (§B7) 가 시계열 모집단 제공.
- **안전 등급**: NHTSA NCAP 만 (US). EuroNCAP / KNCAP 미통합.
  - **Gap 해소**: §C2 EuroNCAP, §C4 KNCAP 스크래핑.

### 🔴 큰 부족
- **L5 Part**: PRD MVP 제외 (post-MVP). 리콜 LLM 추출만 진입.
- **한국 시장 리콜**: API 키 발급 전까지 manual CSV (§B1, §C1).
- **자기인증 / 형식승인**: KATRI 키 부재 — PRD §3.2 에 `events.certifications` 명시되지만 스키마·loader 모두 미구현.
- **부품사 IR**: 개별 부품사 (현대모비스, 만도, 한온시스템 …) IR 본문 미수집. DART 측에 finance 가 있지만 자동차 도메인 cross-reference 안 됨.
- **글로벌 OEM 재무**: SEC EDGAR 미통합 (§A7) — Tesla/Ford/GM/Toyota cross_domain QA 가 한국 OEM 한정.

### 📊 평가 데이터
- **Cross-Domain QA L1~L4 층화 라벨**: gold dataset 에 분류 라벨 미포함 — 사람 라벨링 필요.
- **multi-hop 비율**: PRD 목표 검증용 표본 수 부족 (gold 13건 → 50건+ 필요).

---

## 11. 결론 및 우선순위 권장 (재정리)

### 즉시 가능 (Tier A — 코드만)
1. **§A4 EPA fueleconomy.gov** — `spec.efficiency.*` + `spec.engine.*` 풍부화. PRD §10.9 직격.
2. **§A1 NHTSA Investigations** — 결함 시계열 깊이 보강.
3. **§A2 NHTSA TSB Socrata** — narrative 청크 추가.
4. **§A7 SEC EDGAR (글로벌 OEM)** — cross_domain QA 의 글로벌 확장.
5. **§A6 DBpedia** — Wikidata 부족분 보완.

### 키 발급 후 가능 (Tier B — 사용자 1회 회원가입)
6. **§B1 국토부 자동차 리콜정보 API** — 한국 시장 리콜 진입.
7. **§B4 KOSIS 자동차 통계** — 시장 시계열.

### 스크래핑 (Tier C — 보수적 + 라이선스 확인)
8. **§C2 EuroNCAP** — 유럽 안전 등급.
9. **§C4 KNCAP** — 한국 안전 등급.
10. **§C5 나무위키** (NC 한정) — 한국어 narrative 풍부.

### 사용자 직접 액션 필요
- **AI Hub 키** 발급 + 데이터셋 다운로드 승인 (S7).
- **car365.go.kr 데이터프리존** 예약 + 승인 (§B6).
- **KIDI 등급요율** 분기별 PDF 다운 (§C3).

---

## 부록: 검색 자료 (출처)

본 문서 작성에 참조한 공식 페이지 및 학술 논문:

### 공식·정부 페이지
- [NHTSA Datasets and APIs](https://www.nhtsa.gov/nhtsa-datasets-and-apis)
- [NHTSA vPIC](https://vpic.nhtsa.dot.gov/api/)
- [api.nhtsa.gov 정책](https://api.nhtsa.gov/)
- [NHTSA TSB Socrata 데이터셋](https://catalog.data.gov/dataset/nhtsas-office-of-defects-investigation-odi-technical-service-bulletins-system-tsbs-downloa)
- [NHTSA FARS](https://www.nhtsa.gov/research-data/fatality-analysis-reporting-system-fars)
- [Crash Viewer API](https://crashviewer.nhtsa.dot.gov/CrashAPI)
- [fueleconomy.gov Download](https://www.fueleconomy.gov/feg/download.shtml)
- [fueleconomy.gov Web Services](https://www.fueleconomy.gov/feg/ws/)
- [EPA Annual Certification Data](https://www.epa.gov/compliance-and-fuel-economy-data/annual-certification-data-vehicles-engines-and-equipment)
- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
- [국토교통부_자동차 리콜정보 API](https://www.data.go.kr/data/15089863/openapi.do)
- [국토교통부_자동차종합정보 API](https://www.data.go.kr/data/15071233/openapi.do)
- [KOSIS 공유서비스](https://kosis.kr/openapi/)
- [KOTSA TS 데이터 개방센터](https://www.kotsa.or.kr/portal/contents.do?menuCode=03030200)
- [무공해차 통합누리집 ev.or.kr](https://ev.or.kr/)
- [국토교통부 통계누리 자동차등록](https://stat.molit.go.kr/portal/cate/statMetaView.do?hRsId=58)
- [KAICA 한국자동차산업협동조합](https://www.kaica.or.kr/)
- [KAMA 한국자동차산업협회](https://www.kama.or.kr/)
- [Automotive Ontology Working Group (W3C)](https://www.automotive-ontology.org/)
- [edmcouncil/auto OWL ontology](https://github.com/edmcouncil/auto)
- [Open Charge Map API](https://openchargemap.org/site/develop/api)

### 학술 논문 / 데이터셋
- [arXiv 2411.19539 — Graph RAG for Automobile Failure Analysis](https://arxiv.org/abs/2411.19539)
- [arXiv 2504.01248 — Factual Benchmarking for In-Car LLMs](https://arxiv.org/abs/2504.01248)
- [arXiv 2012.02558 — PLMs for Automotive Complaint Analysis](https://arxiv.org/pdf/2012.02558)
- [arXiv 2107.10609 — Supply Chain Graph Representation Learning](https://arxiv.org/pdf/2107.10609)
- [arXiv 2305.08506 — KG for Supply Chain Resilience](https://arxiv.org/pdf/2305.08506)
- [MDPI Electronics 2025 — Document GraphRAG for Manufacturing](https://www.mdpi.com/2079-9292/14/11/2102)
- [arXiv 2409.20010 — Domain-centric KG with LLMs (Automotive Electrical)](https://arxiv.org/pdf/2409.20010)
- [ACM AIAA 2024 — NER of NEV Parts via LLM](https://dl.acm.org/doi/10.1145/3700523.3700532)
- [PMC 2024 — Chinese NER for Auto Fault Texts](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11854445/)
- [wey-gu/supplychain-dataset-gen (Apache 2.0)](https://github.com/wey-gu/supplychain-dataset-gen)
- [lovit/namuwikitext (CC BY-NC-SA 2.0 KR)](https://github.com/lovit/namuwikitext)
- [나무위키 DB Dumps (Internet Archive)](https://archive.org/details/namuwikidumps)
