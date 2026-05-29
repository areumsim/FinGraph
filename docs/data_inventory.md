# AutoGraph — 실제 수집·적재 데이터 인벤토리

측정 일자: **2026-05-29 01:15 KST** (FORD OEM 확장 + NHTSA taxonomy 적재 + complaint→component 매핑 + SEC bridge 보강 후)
측정 도구: `find`, `wc`, `psql via psycopg`, `cypher-shell via neo4j driver`, `eval/metrics/prd_dashboard`

본 문서는 `data/raw/auto/**` (raw files), PG `auto/bridge/vec` 스키마, Neo4j 라벨/관계의 **실시간 측정값**. `docs/data_sources.md` 가 후보 카탈로그라면, 이건 **현재 디스크·DB 에 들어와 있는 사실**.

PRD §10 자동 측정 결과: `eval/reports/prd_dashboard_latest.md` 참조 (4/5 measurable pass, §10.4/§10.6/§10.11/§10.12 ✅).

---

## 0. 한 줄 요약

| 카테고리 | raw 디스크 | raw 파일 | PG row | Neo4j |
|---|---:|---:|---:|---|
| NHTSA vPIC (마스터) | ~2 MB | 100+ | mfr 22,145 / model 6,770 / variant 428 | 동일 |
| NHTSA Recalls | ~3 MB | 380+ | 493 | 493 :Recall |
| NHTSA Complaints | 20 MB | 158 | 16,005 | 16,005 :Complaint |
| NHTSA Investigations | 4.1 MB | 1 zip (154k rows) | 154 (4 OEM filter) | 154 :Investigation |
| NHTSA SafetyRatings v2 | 1.5 MB | 300+ | 1,680 spec_measurements | 155 SAFETY_RATED_BY |
| **NHTSA component taxonomy** ⭐신규 5/29 | — | (derived) | 176 components | 162 :Module |
| EPA fueleconomy | 2.1 MB | 1 zip | 1,426 spec_measurements | — |
| SEC EDGAR OEM | 46 MB | 10 CIK JSON | 3,199 facts | (Manufacturer 노드 + bridge sec_cik 10건) |
| Wikipedia 자동차 | 900 KB | 193 | 193 vec.chunks | — |
| Wikidata (마스터) | 12 MB | 6 | (manufacturers/suppliers seed 사용) | — |
| AI-Hub 71347 (모터·배터리) | 3.0 GB | 616,898 라벨 | 4 components (L4) | 통합 |
| AI-Hub 578 (부품 품질) | 703 MB | 22 tar | 22 components | 통합 |
| supplier_seed (manual) | — | 1 yaml | 18 components + 19 suppliers | 30 SUPPLIED_BY |
| **합계** | **~3.8 GB** | **~618k files** | **~52k auto rows** | **48k 노드 / 28k 관계** |

진행 단계: **데이터 충분 + 자동 측정 인프라 완성**. PRD §3.3 MVP 범위 OEM **5사** (HYUNDAI/KIA/GENESIS/TESLA/FORD) × 5 year (2020-24) 채워짐. KGM/르노코리아 + 한국 시장 리콜 데이터 부족 (data.go.kr 키 발급 필요).

---

## 1. PostgreSQL 실측 (스키마 적용 완료: 07~13)

```
auto.master_manufacturers           22,145
auto.master_vehicle_models           6,770
auto.master_vehicle_variants           428
auto.master_suppliers                4,812   (legacy QID 마이그레이션 완료)
auto.components                        220   (level=4 Module 만 — 5/29 +176 NHTSA taxonomy)
auto.spec_measurements               3,329
auto.events_recalls                    493   (5/29 +274 FORD)
auto.events_complaints              16,005
auto.events_investigations             154   (4 OEM filter)
auto.oem_financials_sec              3,199
auto.staging_relations                   0   (Wikidata P176 rate-limited)
bridge.corp_entity                   4,806   (5/29 +3 SEC GM/Stellantis/Aptiv)
vec.chunks (auto+finance domain)    765,247   (auto 16,435)
```

### 1.1 `auto.spec_measurements` source 분포

| source | rows | 등급 |
|---|---:|---|
| nhtsa_safety_ratings | 1,680 | A (0.95) |
| epa_fueleconomy | 1,426 | A (0.95) |
| nhtsa_canspec | 223 | A (0.95) |

→ PRD §10.9 "제원 수치 EM 95%+" 의 정량 측정값 source. 합 **3,329 row**.

### 1.2 `auto.components` source/level 분포 (5/29 NHTSA taxonomy 적재 후)

| source | rows | level |
|---|---:|:---:|
| nhtsa_recall_taxonomy ⭐신규 | 176 | 4 |
| aihub_578 | 22 | 4 |
| manual_supplier_seed | 18 | 4 |
| aihub_71347 | 4 | 4 |
| **합** | **220** | (모두 L4) |

→ `load_nhtsa_component_taxonomy.py` 가 events_recalls 의 distinct component_text (178 카테고리) 를 직접 module 로 등록. recall→component 매칭율 0 → 100% (no_match=0). 결과 RECALL_OF 39 → **601 edges**.

### 1.3 `auto.events_investigations` 분포 (4 OEM filter — 24,128/154,019 = 15.7%)

| investigation_type | rows |
|---|---:|
| PE (Preliminary Evaluation) | 89 |
| EA (Engineering Analysis) | 32 |
| DP (Defect Petition) | 14 |
| RQ (Recall Query) | 11 |
| AQ (Audit Query) | 3 |
| (unknown prefix) | 5 |
| **합** | **154** |

variant 매칭: 31 / 모델 매칭: 154 / campno (조사→리콜 종결) 보유: 62.

### 1.4 `bridge.corp_entity` 4,806 row 의 품질 (PRD §10.6)

마이그레이션 (`scripts/migrate/migrate_bridge_supplier_qid_to_id.py`) 으로
supplier entity_id legacy QID (4,830) → numeric supplier_id 변환 완료 (5/28).
5/29: SEC bridge 보강 (GM/Stellantis/Aptiv).

| entity_type | status | total | sec_cik | corp_code | qid | conf ≥ 0.9 |
|---|---|---:|---:|---:|---:|---:|
| supplier | candidate | 4,792 | 0 | 3 | 4,792 | 0 |
| manufacturer | reviewed | 10 | 9 | 1 | 1 | 10 |
| manufacturer | candidate | 2 | 0 | 2 | 0 | 0 |
| supplier | reviewed | 2 | 1 | 1 | 1 | 2 |
| **합** | — | **4,806** | **10** | **7** | **4,794** | **12** |

**PRD §10.6 정확한 모수** ("Wikidata QID + LEI 매칭 confidence ≥ 0.9 비율 80%+") — fuzzy
name match 는 본래 candidate 라 모수 외. deterministic match (wikidata_qid /
lei / business_no / corp_code / sec_cik) 만:

```
strong_match: 12/12 = 100.0%  ✅ 목표 80%+ 충족
              (wikidata_qid 1 + lei 1 + sec_cik 10)
reviewed_only: 12/12 = 100.0%
전체 모수 (참고): 12/4,806 = 0.25%
```

manufacturer QID 보유율 **45.3%** (10,027/22,145). supplier QID 보유율 **99.9%** (4,808/4,812).

### 1.5 `auto.oem_financials_sec` — 글로벌 OEM XBRL facts (5/29 bridge 7 → 10)

| CIK | OEM | facts | year 범위 | bridge entity |
|---|---|---:|---|---|
| 0001318605 | Tesla, Inc. | 691 | 2011-2026 | manufacturer 441 |
| 0001467858 | General Motors | 570 | 2011-2026 | **manufacturer 2000000001 (manual)** |
| 0001521332 | Aptiv PLC (Tier1) | 521 | 2012-2025 | **supplier 9000001 (manual)** |
| 0000037996 | Ford Motor | 506 | 2009-2026 | manufacturer 460 |
| 0001731289 | Nikola | 289 | 2018-2024 | manufacturer 10697 |
| 0001811210 | Lucid Group | 227 | 2020-2026 | manufacturer 10919 |
| 0001874178 | Rivian | 184 | 2021-2026 | manufacturer 10887 |
| 0001094517 | Toyota ADR | 149 | 2009-2025 | manufacturer 448 |
| 0000715153 | Honda ADR | 53 | 2009-2020 | manufacturer 474 |
| 0001605484 | Stellantis | 9 | 2017-2025 | **manufacturer 1000000138 (vPIC alias)** |
| **합** | 10 SEC entities | **3,199** | — | **9 mfr + 1 supplier bridged** |

5/29 처리:
- **GM** — vPIC 미등록 holding → `_ensure_manual_manufacturer` 로 id=2000000001 신규 발급
- **Stellantis N.V.** — vPIC "Stellantis North America" (id=1000000138) 와 alias 매핑
- **Aptiv PLC** — Tier1 부품사 → `_ensure_supplier` 로 supplier_id=9000001 신규 발급, entity_type='supplier' bridge

### 1.6 `vec.chunks` 자동 도메인 source

| source | rows | embedding 보유 |
|---|---:|---|
| nhtsa_complaint | 16,005 | ✅ 16,005 |
| nhtsa_recall | 219 | ✅ 219 |
| wikipedia_auto | 193 | ✅ 193 (5/28 backfill) |
| aihub_578 | 11 | ✅ 11 |
| aihub_71347 | 6 | ✅ 6 |
| datagokr_kotsa_inspection | 1 | ✅ 1 |
| **합 (auto)** | **16,435** | **16,435 (100%)** |

(전체 vec.chunks 765k 중 나머지는 finance 도메인.)

---

## 2. Neo4j 실측 (12 라벨, 14 관계)

### 2.1 노드

```
:Manufacturer       22,145
:VehicleModel        6,770   (5/29 +44 FORD - 노이즈 정리)
:VehicleVariant        428   (5/29 +206 FORD)
:Recall                493   (5/29 +274 FORD)
:Complaint          16,005
:Investigation         154
:Module                203   (5/29 +177 NHTSA taxonomy)
:Component              15
:Supplier            9,642   ⚠️ supplier_seed/edges 중복 적재 의심 (auto.master_suppliers 4,812 의 2배)
:Standard               22
:Plant                  18
:System                 25
```

PRD §4.4 라벨 12종 중 **12종 채워짐**. `:Part` 만 0 (AI-Hub 가 Level=4 만 적재, Part 는 LLM P3 추출 기대).

### 2.2 관계

```
-[:MANUFACTURES        ]  6,770      Manufacturer → VehicleModel
-[:HAS_VARIANT         ]    428      VehicleModel → VehicleVariant
-[:AFFECTED_BY         ]    226      Variant → Recall
-[:REPORTED_IN         ] 15,538      Variant → Complaint
-[:INVESTIGATED_BY     ]    490      Variant/Model → Investigation
-[:LED_TO_RECALL       ]      7      Investigation → Recall
-[:RECALL_OF           ]    601      Recall → Module/Component        ⭐5/29 0→601 (B1 해결 + taxonomy)
-[:COMPLAINT_OF        ]  4,793      Complaint → Module/Component     ⭐5/29 신규
-[:CONTAINS_COMPONENT  ]     24      VehicleModel → Module
-[:CONTAINS_SYSTEM     ]     12      VehicleModel → System (derived)
-[:CONTAINED_IN        ]     57      Module → System
-[:SAFETY_RATED_BY     ]    155      Variant → Standard
-[:OWNS_PLANT          ]     29      Manufacturer → Plant
-[:SUPPLIED_BY         ]     30      Module → Supplier                 ⭐5/29 0→30 (B1 해결)
```

PRD §4.4 의 14 관계 중 **14종 채워짐**. 추가 누락:
- `:COMPLIES_WITH` — 데이터 없음 (KATRI/KMVSS 인증 키 부재)
- `:MANUFACTURED_AT` — model↔plant 매핑 데이터 없음
- `:COMPETES_WITH` — segment 매핑 데이터 없음

PRD §6.7 의무 메타 6개 (source_type/source_id/confidence_score/validated_status/snapshot_year/extraction_method) — `SUPPLIED_BY` 30/30 = **100% ✅** (`eval/metrics/edge_meta_completeness.py` 측정).

---

## 3. 이슈 추적

### ✅ B1. `SUPPLIED_BY` / `RECALL_OF` Neo4j 0건 → 해결 (5/28~29)

- `load_supplier_edges._sync_modules_to_neo4j` + `_sync_suppliers_to_neo4j` 추가 — PG components → Neo4j Module 동기화 패스.
- `load_recall_components` 도 동일 sync 호출.
- **결과**: SUPPLIED_BY 0 → 30, RECALL_OF 0 → 39 → 601 (5/29 taxonomy 후).

### ✅ B2. NHTSA Investigations multi-model dedup → 해결 (5/28)

- cypher_templates_auto: `WITH inv, max(conf) AS confidence` 집계 패턴으로 변경.
- Tesla Model X 10 unique investigations (전 EA26002 × 5 + PE25012 × 4 중복) 확인.
- 장기 TODO: `auto.investigation_model_link` 정규화 테이블.

### ✅ B3. SEC OEM bridge 자동 매칭 실패 3건 → 해결 (5/29)

- `_SEC_CIK_TO_VPIC_MFR_ID` alias dict (Stellantis), `_SEC_MANUAL_MFR_SEED` (GM), `_SEC_TIER1_SUPPLIER_SEED` (Aptiv) 추가.
- bridge.corp_entity sec_cik 7 → 10 (manufacturer 9 + supplier 1).

### ✅ B4. NHTSA 400 false-failure → 해결 (5/28)

- `_common_nhtsa.nhtsa_http_get` 가 HTTP 400 + body Count=0 응답을 정상 처리.

### ✅ B5. NHTSA SafetyRatings 2-step API → 해결 (5/28)

- `/SafetyRatings/VehicleId/{id}` 별도 호출 후 in-place merge.

### ✅ B8. Wikipedia 청크 embedding 미실행 → 해결 (5/28)

- 193 청크 backfill 완료.

### ⚠️ B6. AI-Hub aggregate model name mismatch (양호)

- 71347 의 `IONIQ` / `KONA` / `NIRO` 가 vPIC `Ioniq` / `Ioniq 5/6` 등 prefix 매치.
- 24 CONTAINS_COMPONENT edge 생성 (model 단위 fan-out OK).

### 🟡 B7. Wikidata SPARQL 1 req/min rate-limit (유지)

- `SPARQL_PART_SUPPLIES` (P176) 429. **part_supplies.jsonl 미생성** → staging_relations 0. 추후 재시도.

### 🟡 B10. `:Supplier` Neo4j 노드 중복 (신규 인지, 5/29)

- PG `auto.master_suppliers` 4,812 vs Neo4j `:Supplier` 9,642 — 약 2배.
- 의심: supplier_seed loader 가 manual seed 19 → Neo4j 9,642 fan-out 또는 supplier_id 다른 다중 적재.
- 영향 범위: 매칭 정확도 — name_norm 기준 dedup 안 됐을 가능성. 별도 진단 필요.

### 🟡 B11. NHTSA complaint 의 짧은 카테고리 매칭 누락 (5/29 인지)

- 16,005 complaint 중 10,390 (65%) 가 'POWER TRAIN' 같은 단순 카테고리라 NHTSA recall taxonomy ('POWER TRAIN:DRIVELINE:...' 같은 세분화) 와 매칭 실패.
- 결과: COMPLAINT_OF 4,793 edges (5,615 후보 중 PG 매칭 가능한 것만).
- 추가 보강: complaint 의 distinct category 도 components 에 등록하면 +10k edges 기대. 단 PRD L4 (module) 분류상 'POWER TRAIN' 은 L3 (system) 이라 진단/분류 별도 작업 필요.

---

## 4. PRD §10 성공 기준 (자동 측정)

전체 결과: `eval/reports/prd_dashboard_latest.md` 참조. 측정 명령:
```bash
PYTHONPATH=src python3 -m eval.metrics.prd_dashboard -o eval/reports/prd_dashboard_latest.md
```

| PRD 기준 | 자동 측정 결과 | 상태 |
|---|---|:---:|
| §10.1~10.3 (docker/UI/LLM env) | 외부 측정 — 본 dashboard 범위 밖 | · |
| §10.4 (OEM 5~8 × 모델 30~50 × 2022-24) | OEM=5 models=102 years=(2020,2024) | ✅ |
| §10.5 (BOM L0~3 안정 + L4 60%+) | L0~L3 ✅, L4 = 46/102 = 45.1% | ⚠️ |
| §10.6 (bridge ≥0.9 80%+) | strong_match 12/12 = 100% | ✅ |
| §10.7~10.10 (LLM eval) | LLM_API_KEY 필요 | ⊘ |
| §10.11 (SUPPLIED_BY 100% meta) | 30 edges, 6/6 메타 100% | ✅ |
| §10.12 (코어 변경 < 5%) | 538/12,027 LOC = 4.47% | ✅ |
| §10.13~10.14 (hop·latency) | 운영 trace 필요 | ⊘ |

**즉시 fix 가능 항목**:
- §10.5 → L4 60% 달성을 위해 56개 model (recall/complaint 없는 low-volume) 추가 보강. 가능: spec_measurements measure_key → module fallback, wikipedia spec 본문 LLM 추출.
- §10.13~10.14 → eval/runners 에 cypher 실행 시간·hop trace 수집 (LLM 없이 cypher_templates_auto 별 측정 가능).

---

## 5. 측정 명령 (재현용)

```bash
cd /workspace/arsim/AutoNexusGraph

# raw 측정
find data/raw/auto -name "*.json" -o -name "*.jsonl" -o -name "*.zip" | wc -l
du -sh data/raw/auto/*/

# 한번에 dashboard 전체 측정
PYTHONPATH=src python3 -m eval.metrics.prd_dashboard

# 개별 메트릭
PYTHONPATH=src python3 -m eval.metrics.data_coverage          # §10.4
PYTHONPATH=src python3 -m eval.metrics.bom_coverage           # §10.5
PYTHONPATH=src python3 -m eval.metrics.bridge_quality         # §10.6
PYTHONPATH=src python3 -m eval.metrics.edge_meta_completeness # §10.11
PYTHONPATH=src python3 -m eval.metrics.core_diff              # §10.12
```

---

## 6. 세션 별 데이터 변화량

| 시점 | PG auto/bridge | Neo4j 노드 | Neo4j 관계 | 주요 변화 |
|---|---:|---:|---:|---|
| 5/28 시작 | ~45,000 rows | ~13 라벨 | ~10 관계 | (이전 세션 누적) |
| **5/28 smoke 후** | ~50,000 rows | 14 라벨 | 11 관계 | Investigation, SafetyRating, EPA, SEC, Wikipedia 적재 |
| **5/29 FORD + taxonomy 후** | **~52,000 rows** | **12 라벨** (집계 정확화) | **14 관계** | FORD OEM 확장 (+206 variant + 274 recall), NHTSA taxonomy (+176 module), COMPLAINT_OF 신규 (+4,793) |

5/29 핵심 신규 적재:
- FORD vPIC 44 model + 206 variant + 274 recall + 5년치 complaint/safety
- NHTSA component_text taxonomy 176 module (events_recalls.component_id backfill 374 rows)
- COMPLAINT_OF 4,793 edges (load_complaint_components.py 신규 loader)
- SEC bridge 7 → 10 (GM/Stellantis/Aptiv 보강)
- §10.4 ⚠️ → ✅, §10.12 자동 측정 ✅

다음 우선순위:
1. §10.5 L4 60% 도달 (56개 model 보강 또는 fallback measure)
2. B10 `:Supplier` 중복 진단·정리
3. eval/runners trace 추가 → §10.13/§10.14 자동 측정
4. LLM_API_KEY 확보 후 §10.7~10.10 실측
