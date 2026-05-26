.PHONY: help install fmt lint test test-int up down logs clean \
        ingest-corp ingest-krx ingest-ecos ingest-all

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
	@echo ""
	@echo "  ingest-corp     DART 회사 코드 마스터 다운로드"
	@echo "  ingest-krx      KRX 상장사 + KOSPI200/KOSDAQ100 구성 종목"
	@echo "  ingest-ecos     ECOS 거시지표"
	@echo "  ingest-all      위 3종 일괄"
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

ingest-corp:
	$(PYTHON) scripts/ingest/download_corp_codes.py

ingest-krx:
	$(PYTHON) scripts/ingest/download_listings.py

ingest-ecos:
	$(PYTHON) scripts/ingest/download_ecos.py

ingest-all: ingest-corp ingest-krx ingest-ecos

clean:
	find . -type d -name __pycache__ -not -path './_legacy/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
