"""한국은행 ECOS API 클라이언트.

https://ecos.bok.or.kr/api/

PRD §3.2: 거시지표 (기준금리, 환율, 주요 산업지표).
시계열 데이터 → 거시 컨텍스트 노드 (Neo4j 시점 노드) 또는 PostgreSQL 적재.

엔드포인트 패턴:
  /StatisticSearch/{API_KEY}/json/kr/{start_row}/{end_row}/{stat_code}/{cycle}/{start}/{end}/[item_codes...]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class EcosError(Exception):
    pass


# 자주 쓰는 통계 코드 (코드/주기/대표 시계열)
# 전체 목록: https://ecos.bok.or.kr/api/#/DevGuide/StatisticalCode
KEY_STATS: dict[str, dict[str, str]] = {
    "base_rate":      {"stat_code": "722Y001", "cycle": "D", "item": "0101000"},   # 한국은행 기준금리
    "usd_krw":        {"stat_code": "731Y001", "cycle": "D", "item": "0000001"},   # 원/달러 환율
    "cpi":            {"stat_code": "901Y009", "cycle": "M", "item": "0"},          # 소비자물가지수
    "industry_index": {"stat_code": "901Y033", "cycle": "M", "item": "I11A"},      # 광공업 생산지수
    "gdp":            {"stat_code": "200Y001", "cycle": "Q", "item": "10101"},     # 실질 GDP
}


@dataclass(frozen=True)
class EcosSeries:
    """ECOS 시계열 1포인트."""

    stat_code: str
    item_code1: str
    time: str            # 주기별 형식 (D=YYYYMMDD, M=YYYYMM, Q=YYYYQ#, A=YYYY)
    value: float | None
    unit: str | None
    stat_name: str | None


class EcosClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://ecos.bok.or.kr/api",
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError(
                "ECOS_API_KEY 가 필요합니다. ecos.bok.or.kr 가입 후 .env 에 설정하세요."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def __enter__(self) -> "EcosClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, EcosError)),
        wait=wait_exponential(min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def get_statistic(
        self,
        stat_code: str,
        start: str,
        end: str,
        cycle: str = "M",
        item_code1: str = "",
        item_code2: str = "",
        item_code3: str = "",
        page: int = 1,
        page_size: int = 1000,
    ) -> list[EcosSeries]:
        """단일 시계열 조회.

        Args:
            stat_code: 통계 코드 (예: "722Y001" = 기준금리)
            start, end: 주기별 형식. D=YYYYMMDD, M=YYYYMM, Q=YYYYQN, A=YYYY
            cycle: D | M | Q | SA | A
            item_code1~3: 통계 항목 코드 (생략 가능)
        """
        path_parts = [
            "StatisticSearch",
            self.api_key,
            "json",
            "kr",
            str(page),
            str(page + page_size - 1),
            stat_code,
            cycle,
            start,
            end,
        ]
        if item_code1:
            path_parts.append(item_code1)
        if item_code2:
            path_parts.append(item_code2)
        if item_code3:
            path_parts.append(item_code3)

        url = f"{self.base_url}/{'/'.join(path_parts)}"
        resp = self._client.get(url)
        resp.raise_for_status()
        data = resp.json()

        # ECOS 응답 구조: { "StatisticSearch": { "list_total_count": N, "row": [...] } } 또는 { "RESULT": { "CODE": "...", "MESSAGE": "..." } } (에러)
        if "RESULT" in data:
            raise EcosError(
                f"ECOS error [{data['RESULT'].get('CODE')}] {data['RESULT'].get('MESSAGE')}"
            )
        rows = data.get("StatisticSearch", {}).get("row", [])
        return [
            EcosSeries(
                stat_code=row.get("STAT_CODE", stat_code),
                item_code1=row.get("ITEM_CODE1", ""),
                time=row.get("TIME", ""),
                value=_parse_float(row.get("DATA_VALUE")),
                unit=row.get("UNIT_NAME"),
                stat_name=row.get("STAT_NAME"),
            )
            for row in rows
        ]

    def get_named(self, name: str, start: str, end: str) -> list[EcosSeries]:
        """KEY_STATS 별칭으로 호출 (예: name='base_rate')."""
        if name not in KEY_STATS:
            raise ValueError(f"unknown stat name: {name}. {list(KEY_STATS)}")
        meta = KEY_STATS[name]
        return self.get_statistic(
            stat_code=meta["stat_code"],
            start=start,
            end=end,
            cycle=meta["cycle"],
            item_code1=meta["item"],
        )


def _parse_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None
