"""중앙 설정 — .env 자동 로드, Pydantic 타입 검증."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # === LLM Provider ===
    llm_provider: Literal["openai", "anthropic", "local"] = "openai"
    llm_model: str = "gpt-4o"
    llm_api_key: str = ""
    llm_timeout: float = 120.0

    llm_model_triage: str = "gpt-4o-mini"
    llm_model_planner: str = "claude-sonnet-4-5"
    llm_model_supervisor: str = "gpt-4o-mini"
    llm_model_research: str = "gpt-4o-mini"
    llm_model_graph: str = "claude-sonnet-4-5"
    llm_model_sql: str = "gpt-4o-mini"
    llm_model_calculator: str = "gpt-4o-mini"
    llm_model_validator: str = "gpt-4o-mini"
    llm_model_synthesizer: str = "claude-sonnet-4-5"
    llm_model_judge: str = "gpt-4o"

    local_llm_base_url: str = "http://localhost:8000/v1"

    # === 임베딩 ===
    embedding_url: str = "http://localhost:8080"
    reranker_url: str = "http://localhost:8081"
    embedding_dim: int = 1024

    # === DB ===
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    postgres_dsn: str = "postgresql://autonexusgraph:autonexusgraph_dev@localhost:5432/autonexusgraph"

    # Qdrant — minimal 스택에선 미사용 (pgvector 통합). 활성화 시 .env 에 값 채움.
    qdrant_url: str = ""
    qdrant_api_key: str = ""

    # === 데이터 소스 ===
    dart_api_key: str = ""
    dart_base_url: str = "https://opendart.fss.or.kr/api"

    ecos_api_key: str = ""
    ecos_base_url: str = "https://ecos.bok.or.kr/api"

    krx_base_url: str = "http://data.krx.co.kr"

    # 공공데이터포털 (data.go.kr) — FTC 기업집단·통계청·환경부 등 공통 키
    data_go_kr_api_key: str = ""

    # 통계청 KOSIS — kosis.kr/openapi 무료 키
    kosis_api_key: str = ""

    # 특허청 KIPRIS — kipris.or.kr/kipo-api/ 무료 키
    kipris_api_key: str = ""

    # 한국ESG기준원 (KCGS) — 공개 CSV 다운로드 URL 또는 manual_path
    kcgs_csv_dir: str = "data/raw/kcgs"

    # 빅카인즈 — bigkinds.or.kr 키 (미보유 시 skeleton 만)
    bigkinds_api_key: str = ""

    # LAW.go.kr — 무료 키 (open.law.go.kr/LSO/openApi)
    law_api_key: str = ""

    # SEC EDGAR — 키 불필요 (User-Agent 만 필요)
    sec_user_agent: str = "FinGraph-Research/0.1 (ifkbn@kolon.com)"

    # === 수집 ===
    ingest_tickers: str = "KOSPI200,KOSDAQ100"
    ingest_years_back: int = 3
    ingest_rate_limit_per_sec: float = 10.0
    ingest_raw_dir: Path = Field(default=PROJECT_ROOT / "data" / "raw")
    ingest_processed_dir: Path = Field(default=PROJECT_ROOT / "data" / "processed")

    # === 에이전트 ===
    agent_max_replan: int = 2
    agent_query_budget_sec: int = 40
    agent_max_answer_len: int = 5000
    agent_turn_budget_usd: float = 0.20    # 한 대화 turn 의 최대 LLM 비용

    # === LangGraph checkpoint (PRD §7.5.8) ===
    # auto = PG 시도 → memory 폴백, memory/in_memory = 강제 in-memory, none = 비활성
    langgraph_checkpoint_backend: Literal["auto", "memory", "in_memory", "none"] = "auto"
    langgraph_checkpoint_schema: str = "chat"     # PG schema (search_path 주입)
    langgraph_checkpoint_dsn: str = ""             # 빈 값이면 postgres_dsn 사용

    # === LLM 비용 가드 (사용자 명시) ===
    # 모든 LLM 호출은 dry-run estimator + 누적 한도 + circuit breaker 통과해야 함.
    llm_cost_hard_limit_usd: float = 5.00    # 누적 이 한도 도달 시 즉시 abort
    llm_cost_auto_approve_usd: float = 0.50  # 추정 이 이하면 자동 통과, 초과면 --approve-cost 필요
    llm_cost_report_every: int = 10          # 매 N 호출마다 누적 로그
    llm_cost_log_calls: bool = False          # True 면 ops.llm_calls 에 호출별 상세 적재

    # === Tracing ===
    trace_backend: Literal["langfuse", "langsmith", ""] = ""
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langsmith_api_key: str = ""
    langsmith_project: str = "autonexusgraph"

    # === 운영 ===
    app_env: Literal["local", "server", "production"] = "local"
    log_level: str = "INFO"

    @field_validator("ingest_raw_dir", "ingest_processed_dir", mode="before")
    @classmethod
    def _resolve_path(cls, v: str | Path) -> Path:
        p = Path(v)
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 단위 싱글톤. 테스트에서 override 하려면 cache_clear() 호출."""
    return Settings()
