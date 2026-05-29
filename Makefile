.PHONY: help install fmt lint test test-int up down logs health clean \
        ingest-corp ingest-krx ingest-ecos ingest-targets ingest-bulk \
        ingest-structural ingest-wikidata ingest-wikipedia \
        ingest-news ingest-fss ingest-ftc ingest-kosis \
        ingest-sec ingest-gleif ingest-kipris ingest-law ingest-kcgs \
        serve-embeddings embed-chunks serve-api serve-ui \
        eval-smoke eval-full p3-extract-dry p3-extract p4-load \
        ingest-step1 ingest-step2 ingest-step3 ingest-step4 \
        ingest-step5 ingest-step6 ingest-step7 ingest-step8 \
        ingest-all inventory \
        load-companies load-filings load-financials load-all \
        load-entity-map load-persons load-graph-structural \
        load-wikidata load-wikipedia load-news load-graph-news \
        load-sec load-gleif load-kcgs \
        build-wiki-chunks validate-quality \
        migrate-schema install-agent enable-langgraph trace-on trace-off \
        ingest-auto-vpic ingest-auto-recalls ingest-auto-complaints \
        ingest-auto-wikidata ingest-auto-safety ingest-auto-wikipedia \
        ingest-auto-epa ingest-auto-investigations ingest-auto-sec-oem \
        ingest-auto-mfrcomm ingest-auto-all \
        load-auto-pg load-auto-neo4j load-auto-bridge \
        build-chunks-auto neo4j-init-auto load-auto-all eval-auto \
        load-auto-recall-components load-auto-supplier-edges \
        load-auto-seed-standards-plants load-auto-complaints-neo4j \
        load-auto-aihub load-auto-specs load-auto-safety load-auto-epa \
        load-auto-investigations load-auto-oem-sec load-auto-mfrcomm \
        derive-auto-contains-system load-wikidata-part-supplies \
        extract-auto-p3 extract-auto-p3-cost validate-auto-p4 extract-validate-auto \
        audit-bom-coverage audit-edge-meta audit-dod \
        validate-gold-qa eval-cross \
        ingest-datagokr-recalls ingest-datagokr-inspections \
        ingest-car-go-kr ingest-katri ingest-kncap \
        load-manufactured-at load-datagokr-recalls load-datagokr-inspections \
        load-kncap

# 호스트가 Ubuntu/Debian 계열이면 `python` 없이 `python3` 만 있을 수 있음 — auto-detect.
# 명시 지정하려면: make PYTHON=python3.11 ...
PYTHON ?= $(shell command -v python3 || command -v python || echo python3)
PIP ?= $(shell command -v pip3 || command -v pip || echo pip3)
DOCKER_COMPOSE ?= docker compose

help:
	@echo "FinGraph — 개발/운영 타깃"
	@echo ""
	@echo "  install         pip install -e \".[all]\" (개발용 전체 설치)"
	@echo "  fmt             ruff format"
	@echo "  lint            ruff check + mypy"
	@echo "  test            pytest (integration 제외)"
	@echo "  test-int        pytest -m integration (실제 DB/LLM 필요)"
	@echo ""
	@echo "  up              docker compose up -d (Neo4j + PG + Qdrant)"
	@echo "  down            docker compose down"
	@echo "  logs            docker compose logs -f --tail=100"
	@echo "  health          모든 인프라 (Neo4j/PG/Qdrant/임베딩/DART/ECOS) ping"
	@echo ""
	@echo "  ingest-corp     DART 회사 코드 마스터 다운로드"
	@echo "  ingest-krx      KRX 상장사 + 시가총액 상위 200/100"
	@echo "  ingest-ecos     ECOS 거시지표 (ECOS_API_KEY 필요)"
	@echo "  ingest-targets  corp_code × stock_code 매칭 → ingest_targets.jsonl"
	@echo "  ingest-bulk     KOSPI200+KOSDAQ100 × 3년 일괄 (이어받기·실패추적 지원)"
	@echo "  ingest-docs     사업보고서 원문 zip 다운로드 (~1,149건, ~수 분)"
	@echo "  ingest-all      corp → krx → targets → bulk 전체 순차"
	@echo ""
	@echo "  inventory       data/raw 인벤토리 + 누락 검증"
	@echo ""
	@echo "  load-companies  master.companies 적재"
	@echo "  load-filings    fin.filings 적재"
	@echo "  load-financials fin.financials 적재 (184K+ rows)"
	@echo "  load-all        위 3종 순차 (PG 컨테이너 가동 필요)"
	@echo "  build-chunks    DART zip → vec.chunks (embedding NULL, ~73만 row)"
	@echo "  embed-chunks    BGE-M3 호출 → embedding 채우기 (BGE 서버 필요)"
	@echo "  load-graph      Neo4j Company/Market/Sector/Person 노드 + 관계"
	@echo ""
	@echo "── AutoGraph (자동차 도메인) ──"
	@echo "  ingest-auto-all                   NHTSA vPIC/recalls/complaints/safety + Wikidata"
	@echo "  ingest-auto-safety                NHTSA SafetyRatings (NCAP 5★ 등급)"
	@echo "  ingest-auto-wikipedia             자동차 모델/제조사 Wikipedia 본문 (ko fallback en)"
	@echo "  load-auto-all                     PG → Neo4j 풀 체인 (specs/safety/aihub/seed/recall→comp/derive 포함)"
	@echo "  load-auto-aihub                   AI Hub 71347/578 → :Module + CONTAINS_COMPONENT"
	@echo "  load-auto-specs                   canspec → spec_measurements + variant 보강"
	@echo "  load-auto-safety                  NCAP raw → spec_measurements(safety.*) + SAFETY_RATED_BY"
	@echo "  load-auto-supplier-edges          supplier_seed.yaml → :SUPPLIED_BY (manual A grade)"
	@echo "  load-auto-nhtsa-taxonomy          NHTSA recall component_text → auto.components (level=4)"
	@echo "  load-auto-recall-components       recall.component_text → :RECALL_OF (deterministic)"
	@echo "  load-auto-complaint-components    complaint.components → :COMPLAINT_OF (taxonomy 후행)"
	@echo "  load-auto-seed-standards-plants   :Standard + :Plant + :OWNS_PLANT 시드"
	@echo "  load-auto-complaints-neo4j        :Complaint + :REPORTED_IN"
	@echo "  derive-auto-contains-system       (VehicleModel)-[:CONTAINS_SYSTEM]->(System) 유도 적재"
	@echo "  extract-auto-p3-cost              P3 LLM 비용 dry-run (호출 없이 토큰 추정)"
	@echo "  extract-auto-p3                   P3 LLM 추출 → auto.staging_relations"
	@echo "  validate-auto-p4                  P4 cross-validate → Neo4j 적재"
	@echo "  extract-validate-auto             P3 → P4 한 번에"
	@echo "  eval-auto                         자동차 QA 평가셋 실행"
	@echo "  eval-cross                        Cross-Domain QA (PRD §8.1 CD-L1~L4)"
	@echo ""
	@echo "── DoD audit (PRD §10) ──"
	@echo "  audit-bom-coverage                Level 0~5 노드 + L4 coverage 측정"
	@echo "  audit-edge-meta                   PRD §6.7 의무 메타 invariant (strict)"
	@echo "  audit-dod                         14 항목 트래픽라이트 리포트"
	@echo "  validate-gold-qa                  eval/qa_gold/*.jsonl 스키마/엔티티 lint"
	@echo ""
	@echo "── 외부 데이터 (graceful skip 패턴 — 키 없으면 스킵) ──"
	@echo "  ingest-datagokr-recalls           data.go.kr 15089863 한국 리콜"
	@echo "  ingest-datagokr-inspections       data.go.kr 15155857 수리검사"
	@echo "  ingest-car-go-kr                  [PLACEHOLDER] car.go.kr — 키 미설정 시 raw/auto/car_go_kr/ CSV 수동 다운로드 후 normalize"
	@echo "  ingest-katri                      [PLACEHOLDER] KATRI / bigdata-tic.kr — OAuth client_id/secret 발급 필요"
	@echo "  ingest-kncap                      [PLACEHOLDER] KNCAP — 공식 API 미공개, 수동 CSV 또는 KNCAP_API_KEY 설정 시 동작"
	@echo "  load-manufactured-at              모델↔공장 seed → MANUFACTURED_AT"
	@echo ""
	@echo "  clean           __pycache__/.pytest_cache 삭제"

install:
	$(PIP) install -e ".[all]"

install-agent:                                       # langgraph + tracing 의존성만
	$(PIP) install -e ".[agent]"

enable-langgraph:                                    # 활성화 헬스체크
	@$(PYTHON) -c "from langgraph.graph import StateGraph; print('✓ langgraph import 성공')" || \
	    (echo '✗ langgraph 미설치 — make install-agent 먼저 실행' && exit 1)
	@$(PYTHON) -c "from autonexusgraph.agents.graph import _HAS_LANGGRAPH; \
	    print(f'✓ _HAS_LANGGRAPH = {_HAS_LANGGRAPH}')"
	@$(PYTHON) -c "from autonexusgraph.agents.checkpointer import get_checkpointer; \
	    c = get_checkpointer(); \
	    print(f'✓ checkpointer = {type(c).__name__ if c else None}')"

trace-on:                                            # 환경변수로 tracing 활성 확인
	@echo "TRACE_BACKEND=$${TRACE_BACKEND:-(unset)}"
	@$(PYTHON) -c "from autonexusgraph.agents.tracing import describe_backend; print(describe_backend())"

trace-off:                                           # tracing 비활성 — 환경변수만 unset 안내
	@echo "TRACE_BACKEND 을 빈 값으로 두거나 'none' 으로 설정하세요. (.env 또는 export TRACE_BACKEND=)"

fmt:
	ruff format src tests scripts

lint:
	ruff check src tests scripts
	mypy src

test:
	pytest

test-int:
	pytest -m integration

up:
	$(DOCKER_COMPOSE) up -d
	@echo ""
	@echo "기동됨. 헬스체크:"
	@echo "  Neo4j    : http://localhost:7474"
	@echo "  Postgres : psql -h localhost -U autonexusgraph -d autonexusgraph"
	@echo "  Qdrant   : http://localhost:6333/dashboard"

down:
	$(DOCKER_COMPOSE) down

logs:
	$(DOCKER_COMPOSE) logs -f --tail=100

health:
	$(PYTHON) scripts/healthcheck.py

ingest-corp:
	$(PYTHON) scripts/ingest/download_corp_codes.py

ingest-krx:
	$(PYTHON) scripts/ingest/download_listings.py

ingest-ecos:
	$(PYTHON) scripts/ingest/download_ecos.py

ingest-targets:
	$(PYTHON) scripts/ingest/build_targets.py

ingest-bulk:
	$(PYTHON) scripts/ingest/bulk_dart.py

ingest-docs:
	$(PYTHON) scripts/ingest/download_documents.py

ingest-all: ingest-corp ingest-krx ingest-targets ingest-bulk ingest-ecos

inventory:
	$(PYTHON) scripts/data_inventory.py

load-companies:
	$(PYTHON) scripts/load/load_companies.py

load-filings:
	$(PYTHON) scripts/load/load_filings.py

load-financials:
	$(PYTHON) scripts/load/load_financials.py

load-all:
	$(PYTHON) scripts/load/load_all.py

build-chunks:
	$(PYTHON) scripts/load/build_chunks.py

# NOTE: embed-chunks 의 실제 정의는 line ~183 (EMBEDDING_URL 주입 포함).
# 여기서 중복 선언하면 GNU make 가 첫 정의로 shadow 하므로 별도 정의 두지 않는다.

load-graph:
	$(PYTHON) scripts/load/load_graph_companies.py

migrate-schema:                                      # Neo4j 스키마 정합성 마이그레이션 (README §11.6)
	$(PYTHON) scripts/migrate_neo4j_schema.py

# ── Step별 묶음 target — 데이터 통합 고도화 (천천히 안 터지게) ───────────────
ingest-structural:    ; $(PYTHON) scripts/ingest/bulk_dart_structural.py
ingest-wikidata:      ; $(PYTHON) scripts/ingest/download_wikidata.py
ingest-wikipedia:     ; $(PYTHON) scripts/ingest/download_wikipedia.py
ingest-news:          ; $(PYTHON) scripts/ingest/download_news_rss.py
ingest-fss:           ; $(PYTHON) scripts/ingest/download_fss_press.py
ingest-ftc:           ; $(PYTHON) scripts/ingest/download_ftc_groups.py --year 2024
ingest-kosis:         ; $(PYTHON) scripts/ingest/download_kosis.py
ingest-sec:           ; $(PYTHON) scripts/ingest/download_sec_edgar.py
ingest-gleif:         ; $(PYTHON) scripts/ingest/download_gleif.py
ingest-kipris:        ; $(PYTHON) scripts/ingest/download_kipris.py
ingest-law:           ; $(PYTHON) scripts/ingest/download_law.py
ingest-kcgs:          ; $(PYTHON) scripts/ingest/download_kcgs.py --with-body

load-entity-map:        ; $(PYTHON) scripts/load/load_entity_map.py
load-persons:           ; $(PYTHON) scripts/load/load_persons.py
load-graph-structural:  ; $(PYTHON) scripts/load/load_graph_structural.py
load-wikidata:          ; $(PYTHON) scripts/load/load_wikidata.py
load-wikipedia:         ; $(PYTHON) scripts/load/load_wikipedia.py
load-news:              ; $(PYTHON) scripts/load/load_news.py
load-graph-news:        ; $(PYTHON) scripts/load/load_graph_news_corel.py
load-sec:               ; $(PYTHON) scripts/load/load_sec_edgar.py
load-gleif:             ; $(PYTHON) scripts/load/load_gleif.py
load-kcgs:              ; $(PYTHON) scripts/load/load_kcgs.py --year 2024
build-wiki-chunks:      ; $(PYTHON) scripts/load/build_chunks_wikipedia.py

# ── BGE-M3 임베딩 서버 + backfill ────────────────────────────────────
serve-embeddings:                                    # 별도 터미널에서 띄우기
	CUDA_VISIBLE_DEVICES=0 $(PYTHON) scripts/serve_embeddings.py --embed-port 8080 --no-rerank --host 127.0.0.1

embed-chunks:                                        # vec.chunks.embedding 채우기 (서버 가동 후)
	EMBEDDING_URL=http://127.0.0.1:8080 $(PYTHON) scripts/load/embed_chunks.py --batch-size 64

# ── API + Web UI ────────────────────────────────────────────────────────────
serve-api:                                           # FastAPI /chat 엔드포인트
	$(PYTHON) -m uvicorn autonexusgraph.api.main:app --host 0.0.0.0 --port 31020 --reload

serve-ui:                                            # Streamlit 채팅 UI
	streamlit run src/autonexusgraph/ui/app.py --server.port 31021 --server.address 0.0.0.0

# ── 평가 ────────────────────────────────────────────────────────────────────
eval-smoke:                                          # 3 row 빠른 검증
	$(PYTHON) -m eval.runners.run_qa_eval \
	    --gold eval/qa_gold/gold_qa_v0.example.jsonl \
	    --adapters vector,graph,hybrid,sql_vec --max-cost-usd 0.10

eval-full:                                           # 100문항 풀 매트릭스 (gold 큐레이션 후)
	$(PYTHON) -m eval.runners.run_qa_eval \
	    --gold eval/qa_gold/gold_qa_v0.jsonl \
	    --adapters vector,graph,hybrid,sql_vec \
	    --max-cost-usd 5.00

# ── P3 / P4 LLM 추출 ───────────────────────────────────────────────────────
p3-extract-dry:                                      # 비용 추정만 — LLM 호출 0
	$(PYTHON) scripts/load/extract_business_report_relations.py \
	    --top-by-market-cap 30 --year 2024 --dry-run

p3-extract:                                          # 실제 호출 (가드 통과 후)
	$(PYTHON) scripts/load/extract_business_report_relations.py \
	    --top-by-market-cap 30 --year 2024 --max-cost 1.0

p4-load:
	$(PYTHON) scripts/load/load_validated_relations.py

ingest-step1: ingest-corp ingest-krx ingest-targets        # 마스터
ingest-step2: ingest-bulk ingest-structural                # DART 정형
ingest-step3: ingest-wikidata
ingest-step4: ingest-wikipedia
ingest-step5: ingest-ftc ingest-kosis ingest-fss
ingest-step6: ingest-news
ingest-step7: ingest-sec ingest-gleif ingest-kipris ingest-law
ingest-step8: ingest-kcgs                                  # KCGS 보도자료 모니터
	@echo ""
	@echo "→ KCGS 등급표 CSV 다운로드 후 data/raw/kcgs/<year>/ratings.csv 에 두고 make load-kcgs"

validate-quality:
	$(PYTHON) scripts/validate_cross_source.py

clean:
	find . -type d -name __pycache__ -not -path './_legacy/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info


# ──────────────────────────────────────────────────────────────
# AutoGraph (자동차 도메인 — PRD v2.0)
# ──────────────────────────────────────────────────────────────
# 변수 (override 가능):
#   MAKE   ?= HYUNDAI
#   YEAR   ?= 2024
#   MAKES  ?= HYUNDAI,KIA
#   YEARS  ?= 2022-2024
MAKE  ?= HYUNDAI
YEAR  ?= 2024
MAKES ?= HYUNDAI,KIA,GENESIS
YEARS ?= 2022-2024

ingest-auto-vpic:
	$(PYTHON) -m autograph.ingestion.nhtsa_vpic --makes $(MAKES) --years $(YEARS)

ingest-auto-recalls:
	$(PYTHON) -m autograph.ingestion.nhtsa_recalls --make $(MAKE) --year $(YEAR)

ingest-auto-complaints:
	$(PYTHON) -m autograph.ingestion.nhtsa_complaints --make $(MAKE) --year $(YEAR)

ingest-auto-wikidata:
	$(PYTHON) -m autograph.ingestion.wikidata_auto --all

ingest-auto-safety:
	$(PYTHON) -m autograph.ingestion.nhtsa_safety_ratings --make $(MAKE) --year $(YEAR)

# Wikipedia 자동차 본문 (ko 1차 + en fallback). PG 의 master 테이블에서 entity 리스트 추출.
ingest-auto-wikipedia:
	$(PYTHON) -m autograph.ingestion.wikipedia_auto --all --lang ko --fallback-lang en

# EPA fueleconomy.gov bulk CSV (US 차량 연비·엔진·배출 spec, 키 불필요).
ingest-auto-epa:
	$(PYTHON) -m autograph.ingestion.epa_fueleconomy

# NHTSA ODI Investigations — 리콜 전단계 결함 조사 bulk flat-file (키 불필요, daily).
ingest-auto-investigations:
	$(PYTHON) -m autograph.ingestion.nhtsa_investigations

# SEC EDGAR Company Facts — 글로벌 OEM XBRL 재무 (키 불필요).
ingest-auto-sec-oem:
	$(PYTHON) -m autograph.ingestion.sec_oem

# NHTSA Manufacturer Communications (TSB) — manual download mode (URL retired).
# 사용자 안내 출력 후 종료. 다운로드한 zip 을 data/raw/auto/nhtsa_mfrcomm/ 에 배치.
ingest-auto-mfrcomm:
	$(PYTHON) -m autograph.ingestion.nhtsa_mfrcomm

ingest-auto-all: ingest-auto-vpic ingest-auto-wikidata ingest-auto-recalls ingest-auto-complaints ingest-auto-safety ingest-auto-wikipedia ingest-auto-epa ingest-auto-investigations ingest-auto-sec-oem
	@echo "[autograph] ingest-auto-all done."

neo4j-init-auto:
	$(PYTHON) -m autograph.loaders.neo4j_init

load-auto-pg:
	$(PYTHON) -m autograph.loaders.load_auto_pg --source all

load-auto-neo4j:
	$(PYTHON) -m autograph.loaders.load_auto_neo4j

load-auto-bridge:
	$(PYTHON) -m autograph.loaders.load_bridge

build-chunks-auto:
	$(PYTHON) -m autograph.loaders.build_chunks_auto --source all

# BOM 계층 + 공급망 / 표준 / 공장 / 컴플레인 / 리콜→부품 매칭 (P2 deterministic 추가 패스).
load-auto-nhtsa-taxonomy:
	$(PYTHON) -m autograph.loaders.load_nhtsa_component_taxonomy

load-auto-recall-components:
	$(PYTHON) -m autograph.loaders.load_recall_components

load-auto-complaint-components:
	$(PYTHON) -m autograph.loaders.load_complaint_components

load-auto-supplier-edges:
	$(PYTHON) -m autograph.loaders.load_supplier_edges

load-auto-seed-standards-plants:
	$(PYTHON) -m autograph.loaders.load_seed_standards_plants

load-auto-complaints-neo4j:
	$(PYTHON) -m autograph.loaders.load_complaints_neo4j

load-auto-aihub:
	$(PYTHON) -m autograph.loaders.load_auto_aihub --dataset all

load-auto-specs:
	$(PYTHON) -m autograph.loaders.load_auto_specs

load-auto-safety:
	$(PYTHON) -m autograph.loaders.load_auto_safety

# EPA fueleconomy.gov CSV → spec_measurements. variant 매칭 후 멱등 적재.
load-auto-epa:
	$(PYTHON) -m autograph.loaders.load_auto_epa

# NHTSA ODI Investigations → auto.events_investigations + Neo4j INVESTIGATED_BY.
load-auto-investigations:
	$(PYTHON) -m autograph.loaders.load_auto_investigations

# SEC EDGAR OEM facts → auto.oem_financials_sec + bridge.corp_entity (sec_cik).
load-auto-oem-sec:
	$(PYTHON) -m autograph.loaders.load_auto_oem_sec

# NHTSA TSB / Manufacturer Communications → vec.chunks (source='nhtsa_tsb').
# zip 이 raw 디렉토리에 없으면 안내만 출력. (URL 자동 다운 불가 — manual mode.)
load-auto-mfrcomm:
	$(PYTHON) -m autograph.loaders.load_auto_mfrcomm

# (VehicleModel)-[:CONTAINS_SYSTEM]->(System) — derived after CONTAINS_COMPONENT 적재.
derive-auto-contains-system:
	$(PYTHON) -m autograph.loaders.derive_contains_system

# Wikidata P176 (manufactured by) — 부품↔공급사 staging seed (B 등급 0.80).
# 이후 validate-auto-p4 가 Neo4j SUPPLIED_BY 로 promote.
load-wikidata-part-supplies:
	$(PYTHON) -m autograph.loaders.load_wikidata_part_supplies

# 전체 P2 적재 — 의존 순서를 명시.
#   neo4j-init → master → standards seed → safety/epa → 계층/공급/컴플 → derive → wikidata staging
# load-auto-safety 는 standards 시드 이후 (Standard {code:'NCAP_US'} 노드 필요).
# load-auto-epa 는 variant 마스터 적재 이후 (matching 대상).
# derive-auto-contains-system 은 aihub (CONTAINS_COMPONENT) 이후.
# load-wikidata-part-supplies 는 wikidata raw 적재 이후 — staging 만 채움 (Neo4j 는 P4).
load-auto-all: neo4j-init-auto load-auto-pg load-auto-specs load-auto-neo4j \
               load-auto-bridge load-auto-seed-standards-plants \
               load-auto-safety load-auto-epa load-auto-aihub \
               load-auto-nhtsa-taxonomy \
               load-auto-supplier-edges \
               load-auto-complaints-neo4j load-auto-recall-components \
               load-auto-complaint-components \
               load-auto-investigations load-auto-oem-sec \
               derive-auto-contains-system \
               load-wikidata-part-supplies \
               load-manufactured-at \
               build-chunks-auto
	@echo "[autograph] load-auto-all done."

# ── P3 LLM 추출 + P4 검증 (LLM 호출 비용 발생 — 명시적으로만 실행).
# 비용만 추정 (LLM 호출 안 함): make extract-auto-p3-cost
extract-auto-p3-cost:
	$(PYTHON) -m autograph.extractors.run_p3 \
	    --manufacturer-ids $(MFR_IDS) --limit $(P3_LIMIT) --dry-run-cost

P3_LIMIT ?= 50
MFR_IDS  ?= 498
# 실제 LLM 호출 — hard limit USD (BudgetExceeded 보호).
P3_HARD_LIMIT ?= 2.0
extract-auto-p3:
	$(PYTHON) -m autograph.extractors.run_p3 \
	    --manufacturer-ids $(MFR_IDS) --limit $(P3_LIMIT) \
	    --hard-limit-usd $(P3_HARD_LIMIT)

validate-auto-p4:
	$(PYTHON) -m autograph.extractors.cross_validate

# P3 → P4 → Neo4j (전체).
extract-validate-auto: extract-auto-p3 validate-auto-p4
	@echo "[autograph] P3+P4 done."

eval-auto:
	$(PYTHON) -m eval.runners.run_qa_eval \
	    --gold eval/qa_gold/gold_qa_auto_v0.jsonl \
	    --adapters hybrid \
	    --run-id "auto_$$(date +%Y%m%d_%H%M%S)"

# Cross-Domain QA — PRD §8.1 (CD-L1~L4 4단계 층화) 전용.
eval-cross:
	$(PYTHON) -m eval.runners.run_qa_eval \
	    --gold eval/qa_gold/gold_qa_cross_v0.jsonl \
	    --adapters hybrid \
	    --run-id "cross_$$(date +%Y%m%d_%H%M%S)"

# ─── DoD audit (PRD §10) ─────────────────────────────────────────
audit-bom-coverage:
	$(PYTHON) scripts/audit/bom_coverage.py

audit-edge-meta:
	$(PYTHON) scripts/audit/edge_meta_invariants.py --strict

audit-dod:
	$(PYTHON) scripts/audit/dod_audit.py

validate-gold-qa:
	$(PYTHON) scripts/audit/validate_gold_qa.py eval/qa_gold/*.jsonl

# ─── 외부 데이터 소스 (graceful skip — 키 없으면 0 byte) ───────────
ingest-datagokr-recalls:
	$(PYTHON) -m autograph.ingestion.datagokr_recalls

ingest-datagokr-inspections:
	$(PYTHON) -m autograph.ingestion.datagokr_inspections

ingest-car-go-kr:
	$(PYTHON) -m autograph.ingestion.car_go_kr_recalls

ingest-katri:
	$(PYTHON) -m autograph.ingestion.katri_tic

ingest-kncap:
	$(PYTHON) -m autograph.ingestion.kncap

load-datagokr-recalls:
	$(PYTHON) -m autograph.loaders.load_datagokr_recalls

load-datagokr-inspections:
	$(PYTHON) -m autograph.loaders.load_datagokr_inspections

load-kncap:
	$(PYTHON) -m autograph.loaders.load_kncap

load-manufactured-at:
	$(PYTHON) -m autograph.loaders.load_manufactured_at
