"""KIPRIS plus 특허청 OpenAPI 클라이언트.

API: http://plus.kipris.or.kr/openapi/rest/
키 발급: http://plus.kipris.or.kr/ (회원가입 후 무료)

주요 endpoint:
- 출원인(applicant) 통합검색: /KipoStatistics/applicantSearch
- 회사명 기반 출원 목록: /CorporationSearchService
- 특허 상세: /PatentDetailSearchService
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


KIPRIS_BASE = "http://plus.kipris.or.kr/openapi/rest"


@dataclass(frozen=True)
class Patent:
    application_no: str
    registration_no: str | None
    applicant_name: str
    title: str | None
    filing_date: str | None
    publication_date: str | None
    registration_date: str | None
    ipc_class: str | None
    status: str | None
    raw: dict


class KiprisClient:
    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        if not api_key:
            raise ValueError(
                "KIPRIS_API_KEY 미설정 — plus.kipris.or.kr 에서 무료 키 발급 후 .env 추가"
            )
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def __enter__(self) -> "KiprisClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self._client.close()

    def search_by_applicant(self, applicant_name: str, year_from: int = 2020,
                             page: int = 1, page_size: int = 100) -> dict:
        """출원인 이름으로 특허 출원 목록.

        endpoint 와 파라미터는 KIPRIS API spec 변경 가능 — 첫 호출 시 검증 필요.
        """
        url = f"{KIPRIS_BASE}/CorporationSearchService/getCorporationSearch"
        params = {
            "applicant": applicant_name,
            "startYear": year_from,
            "pageNo": page,
            "numOfRows": page_size,
            "ServiceKey": self.api_key,
        }
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") \
            else {"raw_xml": resp.text}
