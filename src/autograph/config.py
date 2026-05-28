"""AutoGraph 전용 설정 — finance Settings 를 share 하고 자동차 소스 키만 추가.

finance 의 ``fingraph.config.get_settings()`` 와 동일 .env 를 읽는다. extra='ignore' 라
finance Settings 가 모르는 키도 통과. 본 모듈은 AutoGraph 키만 묶어 노출.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AutoSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # === NHTSA (vPIC / Recalls / Complaints) — 키 불필요 ===
    nhtsa_vpic_base_url: str = "https://vpic.nhtsa.dot.gov/api"
    nhtsa_api_base_url: str = "https://api.nhtsa.gov"

    # === Wikidata SPARQL ===
    wikidata_sparql_url: str = "https://query.wikidata.org/sparql"
    wikidata_user_agent: str = "AutoGraph-Research/0.1 (ifkbn@kolon.com)"

    # === 한국 자동차리콜센터 / KATRI / KNCAP — 키 없을 시 graceful skip ===
    car_go_kr_api_key: str = ""
    katri_api_key: str = ""
    kncap_api_key: str = ""

    # === 시험인증 빅데이터 플랫폼 (KATRI 운영) — OAuth client credentials ===
    bigdata_tic_base_url: str = "https://oauth.bigdata-tic.kr"
    bigdata_tic_client_id: str = ""
    bigdata_tic_client_secret: str = ""

    # === 한국교통안전공단 수리검사내역 (data.go.kr 15155857 파일 다운) ===
    datagokr_kotsa_inspection_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "raw" / "datagokr",
    )

    # === 데이터 루트 (finance 와 공유 가능, 자동차는 subdir 'auto/') ===
    auto_raw_dir: Path = Field(default=PROJECT_ROOT / "data" / "raw" / "auto")

    # === 수집 범위 기본값 ===
    auto_ingest_makes: str = "HYUNDAI,KIA,GENESIS,TESLA"
    auto_ingest_year_min: int = 2020
    auto_ingest_year_max: int = 2024


@lru_cache(maxsize=1)
def get_auto_settings() -> AutoSettings:
    return AutoSettings()
