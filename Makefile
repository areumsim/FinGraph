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
        ingest-auto-wikidata ingest-auto-all \
        load-auto-pg load-auto-neo4j load-auto-bridge \
        build-chunks-auto neo4j-init-auto load-auto-all eval-auto

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
	@echo "  Postgres : psql -h localhost -U fingraph -d fingraph"
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

ingest-auto-all: ingest-auto-vpic ingest-auto-wikidata ingest-auto-recalls ingest-auto-complaints
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

load-auto-all: neo4j-init-auto load-auto-pg load-auto-neo4j load-auto-bridge build-chunks-auto
	@echo "[autograph] load-auto-all done."

eval-auto:
	$(PYTHON) -m eval.runners.run_qa_eval \
	    --gold eval/qa_gold/gold_qa_auto_v0.jsonl \
	    --adapters hybrid \
	    --run-id "auto_$$(date +%Y%m%d_%H%M%S)"
