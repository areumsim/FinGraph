# `ontology/auto/` — 자동차 도메인 SSOT

본 디렉토리의 YAML 7 개는 **AutoGraph 도메인의 단일 진실 공급원 (Single Source of Truth)**.
Python 로더는 `src/autograph/ontology.py` 하나이며, 본 디렉토리를 *읽기 전용* 으로 다룬다.

> **finance 도메인 ontology** (`ontology/entities.yaml`, `relations.yaml`, `extractors.yaml`)
> 는 별개의 SSOT. 본 디렉토리는 자동차 도메인 전용이며 두 영역은 서로 영향을 주지 않는다.

---

## 파일별 역할 한 줄 요약

| 파일 | 역할 | 소비처 |
|---|---|---|
| `entities.yaml` | Neo4j 라벨 + key 컬럼 + PG 매핑 + provenance 정의 (11 라벨) | `neo4j_init` 제약 / LLM 프롬프트 entity 표 / P4 검증 |
| `relations.yaml` | Neo4j 관계 + from/to 라벨 + 의무 메타 + **`enabled` 플래그** (14 관계) | `neo4j_init` / LLM 프롬프트 relation 표 / P3 추출 대상 / P4 검증 / cypher 적재 |
| `extractors.yaml` | P2/P3/P4 추출기 카탈로그 — 입력 source · 출력 entity/relation · 구현 경로 · status | 사람의 운영 점검 (자동 소비 없음) |
| `system_taxonomy.yaml` | 차량 시스템 19 종 (POWERTRAIN / BRAKE / ADAS / …) + alias_codes | `:System` 노드 시드 / AI Hub `powertrain` → canonical `POWERTRAIN` 정규화 / LLM 추출 캐치업 |
| `standards.yaml` | FMVSS / ECE / KMVSS / NCAP / KNCAP / UN R155 / ISO 26262 등 22 표준 | `:Standard` 노드 시드 |
| `plants.yaml` | 한국 OEM + 글로벌 공장 18 개 (Hyundai Ulsan / Kia 화성 / Tesla Fremont …) | `:Plant` 노드 시드 + `(:Manufacturer)-[:OWNS_PLANT]->(:Plant)` |
| `supplier_seed.yaml` | A-grade 공급사 시드 — 19 공급사 × 46 (supplier, component, customer) 트리플 | `:Supplier` 노드 + `(:Module|:Part)-[:SUPPLIED_BY]->(:Supplier)` |

---

## 갱신 시 주의 (영향 범위)

### `entities.yaml` 를 수정하면…

- 새 라벨 추가 → `neo4j_init.py` 가 자동으로 CONSTRAINT 생성 (재실행 필요).
- 기존 라벨의 `key` 변경 → **Neo4j 노드 식별 키가 바뀜.** 기존 데이터의 신규 키 백필
  마이그레이션이 필요하다. 위험.
- `description` 만 바꾸는 건 안전 — LLM 프롬프트 entity 표에 영향 (다음 P3 실행부터 반영).

### `relations.yaml` 를 수정하면 (가장 광범위 영향)

- **`enabled` 플래그 (LLM 프롬프트 / Neo4j 적재 / P4 검증 동시 영향)**
  - `enabled: true` 로 변경하면:
    1. `render_relation_table_for_prompt(enabled_only=True)` 가 해당 관계를 LLM 프롬프트
       relation 표에 노출 — LLM 이 해당 관계를 추출하기 시작.
    2. 추출된 후보가 `auto.staging_relations` 에 들어감.
    3. `cross_validate.py::_VALIDATORS` 가 해당 관계 타입을 지원해야 Neo4j 적재까지 진행 —
       지원 안 하면 staging 에만 남고 그래프에 안 들어감. **현재 P4 지원: SUPPLIED_BY,
       RECALL_OF 만.**
  - 다른 관계 (COMPETES_WITH / MANUFACTURED_AT / CONTAINS_MODULE / CONTAINS_PART) 를 켜려면
    `extractors/prompts/relation_extract_auto.yaml` 의 `target_relations` 도 함께 추가하고
    `cross_validate.py::_VALIDATORS` 에 검증 함수를 등록할 것.
- `from` / `to` 라벨 변경 → cypher 템플릿과 cross_validate 의 resolve 함수가 가정하는 라벨이
  달라짐. 회귀 테스트 필수 (`tests/autograph/test_cypher_templates.py`).
- 새 관계 추가 → ontology 만 추가해서는 Neo4j 에 자동 적재되지 않음. 별도 loader 가
  명시적으로 MERGE 해야 함.

### `extractors.yaml`

소비 코드 없음 — 사람 운영 가이드용. 새 추출기 모듈을 추가하면 본 파일에 1 엔트리 추가하는
것이 컨벤션. `status` 필드는 `implemented` 또는 `deferred` 둘 중 하나.

### `system_taxonomy.yaml`

`alias_codes` 가 핵심. AI Hub / 매뉴얼 / LLM 출력에 등장하는 raw 코드 (`powertrain`,
`BAT_PACK`, `에어백` 등) 를 모두 canonical `SCREAMING_SNAKE_CASE` 로 묶는다. 새 alias 를
발견하면 해당 system 의 `alias_codes` 에 추가만 하면 `canonical_system_code()` 가 자동
매칭.

### `standards.yaml` / `plants.yaml`

엣지가 별도 ingest PR 에서 채워진다 — 본 시드는 노드만 만든다. 신규 표준/공장은 그냥
배열에 한 줄 추가. `code` 는 SCREAMING_SNAKE_CASE 유지.

### `supplier_seed.yaml` (가장 빈번하게 갱신될 가능성)

- 한 공급사 묶음에 `components` 리스트 추가 → 다음 `make load-auto-supplier-edges` 부터
  새 엣지 생성.
- `customer` 필드는 메타 보존용 (그 OEM 모델에 한정한 적재가 아니라 모든 매칭 모듈에 적재).
- **잘못된 매핑 발견 시:** 본 파일에서 삭제하지 말고 `confidence: 0.0` 또는 별도
  comment 로 보관 — 다른 데이터셋과 cross-check 후 결정.

---

## 단위 테스트

`tests/autograph/test_ontology.py` 가:

- 모든 라벨에 key 가 정의돼 있는지
- 모든 관계의 from/to 가 정의된 라벨인지
- `edge_required_meta` 가 PRD §6.7 와 일치하는지
- alias canonicalization 이 기대한 매핑을 따르는지
- 필수 핵심 라벨/관계가 모두 등록돼 있는지

를 검증. 본 디렉토리 어떤 yaml 을 고치든 `pytest tests/autograph/test_ontology.py` 를 먼저
돌릴 것.

---

## 다음 단계 (deferred)

| 영역 | 상태 | 어떻게 활성화 |
|---|---|---|
| LLM relations 4 종 (COMPETES_WITH / MANUFACTURED_AT / CONTAINS_MODULE / CONTAINS_PART) | wired, disabled | `relations.yaml::enabled: true` + `prompts/relation_extract_auto.yaml::target_relations` 추가 + `cross_validate._VALIDATORS` 등록 |
| Wikidata P176 자동 SUPPLIED_BY | manual seed 로 대체 중 | 다음 PR — SPARQL 호출 추가 |
| Plant ↔ VehicleModel `MANUFACTURED_AT` | OWNS_PLANT 만 있음 | 모델→공장 데이터 수집 후 별도 loader |
| KNCAP / Euro NCAP / IIHS | NHTSA NCAP 만 구현 (`SAFETY_RATED_BY`) | 평가기관별 별도 ingest 모듈 |
| `COMPLIES_WITH` (차량↔표준) | 표준 노드만 있음 | KNCAP/KATRI ingestion PR |
