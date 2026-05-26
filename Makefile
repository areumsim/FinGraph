.PHONY: help install fmt lint test test-int up down logs health clean \
        ingest-corp ingest-krx ingest-ecos ingest-targets ingest-bulk \
        ingest-all inventory \
        load-companies load-filings load-financials load-all

PYTHON ?= python
PIP ?= pip
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
	@echo "  ingest-all      corp → krx → targets → bulk 전체 순차"
	@echo ""
	@echo "  inventory       data/raw 인벤토리 + 누락 검증"
	@echo ""
	@echo "  load-companies  master.companies 적재"
	@echo "  load-filings    fin.filings 적재"
	@echo "  load-financials fin.financials 적재 (184K+ rows)"
	@echo "  load-all        위 3종 순차 (PG 컨테이너 가동 필요)"
	@echo ""
	@echo "  clean           __pycache__/.pytest_cache 삭제"

install:
	$(PIP) install -e ".[all]"

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

clean:
	find . -type d -name __pycache__ -not -path './_legacy/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
