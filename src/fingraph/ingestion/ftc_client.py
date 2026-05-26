"""공정거래위원회 (FTC) 대규모기업집단현황 클라이언트.

매년 5월 지정 — 자산총액 5조 이상 그룹의 계열사 명단.
https://www.ftc.go.kr/ → 대규모기업집단 → 지정현황

공식 OPI (Open Public Information) API 가 있긴 하나 한정적이라
공시정보시스템(opni.ftc.go.kr) 의 다운로드 URL 사용.
또는 공공데이터포털 (data.go.kr) API.

라이선스: 공공누리 제1유형 (출처표시) — 상업·연구 사용 가능.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class GroupCompany:
    """기업집단 계열사 1건."""

    group_name: str          # 삼성, 현대자동차, ...
    company_name: str        # 삼성전자, 현대자동차 ...
    representative: str | None
    designated_year: int


class FtcClient:
    """공공데이터포털 — 공정거래위원회 기업집단지정 정보 API.

    무료 key 발급 필요: https://www.data.go.kr/data/15083033/openapi.do
    (key 미설정이면 init 에러 — opni.ftc.go.kr 의 CSV/HWP 다운로드 대안 사용 가능)
    """

    BASE_URL = "https://api.odcloud.kr/api/15083033/v1/uddi:7fae72ae-..."

    def __init__(self, api_key: str | None = None, timeout: float = 30.0) -> None:
        # FTC API 키는 .env 추가 필요 — 현재는 옵션. 키 없으면 CSV fallback.
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def __enter__(self) -> "FtcClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch_groups_csv(self, year: int) -> bytes:
        """CSV fallback — 공시자료실 공개 파일 직접 다운로드.

        실제 URL 패턴은 매년 다름. 일단 빈 구현 — Phase 5 에서 정교화.
        지금은 수동 CSV 업로드 경로 권장:
            data/raw/ftc/groups_{year}.csv
        """
        raise NotImplementedError(
            "FTC 자동 다운로드 미구현 — "
            "https://www.ftc.go.kr → 대규모기업집단 → 지정현황 에서 수동 다운로드 후 "
            "data/raw/ftc/groups_{year}.csv 에 배치하세요."
        )
