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

    postgres_dsn: str = "postgresql://fingraph:fingraph_dev@localhost:5432/fingraph"

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""

    # === 데이터 소스 ===
    dart_api_key: str = ""
    dart_base_url: str = "https://opendart.fss.or.kr/api"

    ecos_api_key: str = ""
    ecos_base_url: str = "https://ecos.bok.or.kr/api"

    krx_base_url: str = "http://data.krx.co.kr"

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

    # === Tracing ===
    trace_backend: Literal["langfuse", "langsmith", ""] = ""
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langsmith_api_key: str = ""
    langsmith_project: str = "fingraph"

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
