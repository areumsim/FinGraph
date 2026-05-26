"""KRX 정보데이터시스템 클라이언트.

http://data.krx.co.kr/

PRD §3.2:
- 상장사 마스터 (종목코드/회사명/시장/업종)
- KOSPI200/KOSDAQ100 구성 종목

KRX 는 공식 REST API 가 없고, 웹사이트 내부 POST 엔드포인트(MDC)에
brut JSON 으로 응답하는 패턴을 사용한다.
Step 1: OTP 발급 (`bldAttendant/generate.cmd`)
Step 2: OTP 로 다운로드 (`bldAttendant/download.cmd`)
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import httpx


KRX_GENERATE_URL = "/comm/bldAttendant/generate.cmd"
KRX_DOWNLOAD_URL = "/comm/bldAttendant/executeForResourceBundle.cmd"
KRX_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "http://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd",
    "X-Requested-With": "XMLHttpRequest",
}


@dataclass(frozen=True)
class Listing:
    """상장 종목 1건."""

    stock_code: str          # 6자리 종목코드
    short_code: str          # 단축코드 (= stock_code 인 경우 다수)
    isin: str                # ISIN (12자리)
    name: str
    market: str              # KOSPI / KOSDAQ / KONEX
    sector: str | None       # 업종
    list_date: str | None    # 상장일 YYYYMMDD


class KrxClient:
    """KRX MDC 다운로드 패턴 래퍼."""

    def __init__(
        self,
        base_url: str = "http://data.krx.co.kr",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers=KRX_BROWSER_HEADERS)

    def __enter__(self) -> "KrxClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---- 핵심: bld → OTP → CSV/JSON 다운로드 ----

    def _generate_otp(self, bld: str, **params: Any) -> str:
        """KRX 내부 빌드 ID 로 OTP 발급."""
        resp = self._client.post(
            f"{self.base_url}{KRX_GENERATE_URL}",
            data={"bld": bld, "name": "fileDown", **params},
        )
        resp.raise_for_status()
        return resp.text

    def _download(self, otp: str) -> bytes:
        """OTP 로 본 파일 다운로드 (CSV — EUC-KR)."""
        resp = self._client.post(
            f"{self.base_url}{KRX_DOWNLOAD_URL}",
            data={"code": otp},
        )
        resp.raise_for_status()
        return resp.content

    # ---- 1. 상장 회사 마스터 ----

    def fetch_listed_companies(self, market: str = "ALL"):
        """상장 종목 마스터.

        market: ALL | STK(코스피) | KSQ(코스닥) | KNX(코넥스)
        반환: pandas.DataFrame
        """
        import pandas as pd

        mkt_map = {"ALL": "ALL", "KOSPI": "STK", "STK": "STK",
                   "KOSDAQ": "KSQ", "KSQ": "KSQ", "KONEX": "KNX", "KNX": "KNX"}
        mkt_id = mkt_map.get(market.upper(), "ALL")

        # MDC02501: 종목 시세 (상장 마스터 포함)
        otp = self._generate_otp(
            "dbms/MDC/STAT/standard/MDCSTAT01901",
            mktId=mkt_id,
        )
        raw = self._download(otp)
        df = pd.read_csv(io.BytesIO(raw), encoding="euc-kr")
        return df

    # ---- 2. 지수 구성 종목 (KOSPI200, KOSDAQ100) ----

    def fetch_index_constituents(self, index_name: str = "KOSPI200"):
        """지수 구성 종목.

        index_name: KOSPI200 | KOSDAQ100 | KOSPI100 | ...
        반환: pandas.DataFrame (종목코드/종목명/시가총액 등)
        """
        import pandas as pd

        # MDCSTAT00601 : 지수별 구성종목
        # idxIndCd 매핑은 KRX 사이트에서 조회 — 대표 지수만 사전 정의
        idx_cd_map = {
            "KOSPI200": "1028",
            "KOSPI100": "1034",
            "KOSPI50": "1035",
            "KOSDAQ150": "2203",
            "KOSDAQ100": "2002",   # 참고: KOSDAQ100 은 공식 지수가 아님(KOSDAQ150 권장)
        }
        idx_cd = idx_cd_map.get(index_name.upper())
        if not idx_cd:
            raise ValueError(f"지원하지 않는 지수: {index_name}. {list(idx_cd_map)}")

        otp = self._generate_otp(
            "dbms/MDC/STAT/standard/MDCSTAT00601",
            indIdx=idx_cd[0],
            indIdx2=idx_cd,
        )
        raw = self._download(otp)
        df = pd.read_csv(io.BytesIO(raw), encoding="euc-kr")
        return df
