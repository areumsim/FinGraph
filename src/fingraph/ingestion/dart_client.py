"""DART Open API 클라이언트.

전자공시시스템 (Data Analysis, Retrieval and Transfer System).
https://opendart.fss.or.kr/

PRD §3.2 (데이터 소스):
- 사업/반기/분기 보고서 (본문 임베딩 + 관계 추출)
- 재무제표 XBRL (PostgreSQL 정량 노드)
- 기업 지배구조 보고서 (임원·자회사 관계)

Rate limit: 무료 키 일 10,000건. 부드러운 호출 속도 가드(~10 req/s) 적용.
"""

from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class DartError(Exception):
    """DART API 호출 실패."""


# DART status codes: https://opendart.fss.or.kr/guide/main.do
DART_STATUS_OK = "000"
DART_STATUS_NO_DATA = "013"


@dataclass(frozen=True)
class CorpCode:
    """DART corp_code 마스터 1행."""

    corp_code: str           # 8자리 고유번호
    corp_name: str           # 정식명칭
    stock_code: str | None   # 6자리 종목코드 (상장사만)
    modify_date: str         # YYYYMMDD


@dataclass(frozen=True)
class Filing:
    """공시 보고서 1건."""

    rcept_no: str
    corp_code: str
    corp_name: str
    report_nm: str
    rcept_dt: str            # YYYYMMDD
    flr_nm: str              # 공시 제출인명
    pblntf_ty: str           # 공시 유형 (A=정기공시, ...)


@dataclass(frozen=True)
class FinStatRow:
    """재무제표 1행 (개별/연결 구분은 fs_div)."""

    corp_code: str
    bsns_year: str           # 사업연도
    reprt_code: str          # 보고서 코드 (11011=사업, 11012=반기, ...)
    fs_div: str              # CFS=연결, OFS=별도
    sj_div: str              # BS=재무상태, IS=손익계산서, ...
    account_nm: str
    thstrm_amount: str       # 당기 (문자열 — 음수/콤마 가능)
    frmtrm_amount: str | None
    bfefrmtrm_amount: str | None


class DartClient:
    """DART Open API 동기 클라이언트."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://opendart.fss.or.kr/api",
        timeout: float = 30.0,
        rate_limit_per_sec: float = 10.0,
    ) -> None:
        if not api_key:
            raise ValueError(
                "DART_API_KEY 가 필요합니다. opendart.fss.or.kr 가입 후 .env 에 설정하세요."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._min_interval = 1.0 / rate_limit_per_sec if rate_limit_per_sec > 0 else 0
        self._last_call_at: float = 0.0
        self._client = httpx.Client(timeout=timeout)

    def __enter__(self) -> "DartClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---- 내부 유틸 ----

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call_at = time.monotonic()

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, DartError)),
        wait=wait_exponential(min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        self._throttle()
        all_params = {"crtfc_key": self.api_key, **(params or {})}
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self._client.get(url, params=all_params)
        resp.raise_for_status()
        return resp

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._get(path, params)
        data: dict[str, Any] = resp.json()
        status = data.get("status", DART_STATUS_OK)
        if status not in (DART_STATUS_OK, DART_STATUS_NO_DATA):
            raise DartError(f"DART error [{status}] {data.get('message', '')} (path={path})")
        return data

    # ---- 1. 회사 코드 마스터 ----

    def fetch_corp_codes_zip(self) -> bytes:
        """corpCode.xml.zip 다운로드 (전체 ~2MB)."""
        resp = self._get("corpCode.xml")
        return resp.content

    def parse_corp_codes(self, zip_bytes: bytes) -> Iterator[CorpCode]:
        """zip → CorpCode iterator."""
        import xml.etree.ElementTree as ET

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            xml_name = next(n for n in zf.namelist() if n.endswith(".xml"))
            with zf.open(xml_name) as xf:
                tree = ET.parse(xf)
        for elem in tree.getroot().findall(".//list"):
            stock = (elem.findtext("stock_code") or "").strip()
            yield CorpCode(
                corp_code=(elem.findtext("corp_code") or "").strip(),
                corp_name=(elem.findtext("corp_name") or "").strip(),
                stock_code=stock or None,
                modify_date=(elem.findtext("modify_date") or "").strip(),
            )

    # ---- 2. 회사 기본 정보 ----

    def get_company_info(self, corp_code: str) -> dict[str, Any]:
        """기업 개황 — 영문명, 업종, 대표자, 설립일 등."""
        return self._get_json("company.json", {"corp_code": corp_code})

    # ---- 3. 공시 목록 ----

    def list_filings(
        self,
        corp_code: str | None = None,
        bgn_de: str | None = None,
        end_de: str | None = None,
        pblntf_ty: str = "A",          # A=정기공시
        page_no: int = 1,
        page_count: int = 100,
        **extra: Any,
    ) -> dict[str, Any]:
        """공시 검색 — list.json. 전체 응답 반환 (페이징 메타 포함)."""
        params: dict[str, Any] = {
            "pblntf_ty": pblntf_ty,
            "page_no": page_no,
            "page_count": min(page_count, 100),
            **extra,
        }
        if corp_code:
            params["corp_code"] = corp_code
        if bgn_de:
            params["bgn_de"] = bgn_de
        if end_de:
            params["end_de"] = end_de
        return self._get_json("list.json", params)

    def iter_filings(
        self,
        corp_code: str | None = None,
        bgn_de: str | None = None,
        end_de: str | None = None,
        pblntf_ty: str = "A",
        **extra: Any,
    ) -> Iterator[Filing]:
        """list_filings 전 페이지 순회."""
        page = 1
        while True:
            data = self.list_filings(
                corp_code=corp_code,
                bgn_de=bgn_de,
                end_de=end_de,
                pblntf_ty=pblntf_ty,
                page_no=page,
                page_count=100,
                **extra,
            )
            if data.get("status") == DART_STATUS_NO_DATA:
                return
            for row in data.get("list", []):
                yield Filing(
                    rcept_no=row["rcept_no"],
                    corp_code=row["corp_code"],
                    corp_name=row["corp_name"],
                    report_nm=row["report_nm"],
                    rcept_dt=row["rcept_dt"],
                    flr_nm=row.get("flr_nm", ""),
                    pblntf_ty=row.get("pblntf_ty", ""),
                )
            total_page = int(data.get("total_page", 1))
            if page >= total_page:
                return
            page += 1

    # ---- 4. 공시 원문 다운로드 ----

    def download_filing_document(self, rcept_no: str) -> bytes:
        """document.xml — 보고서 원문 (zip)."""
        resp = self._get("document.xml", {"rcept_no": rcept_no})
        return resp.content

    # ---- 5. 재무제표 ----

    def get_single_finstat_all(
        self,
        corp_code: str,
        bsns_year: str,
        reprt_code: str = "11011",      # 11011=사업, 11012=반기, 11013=1분기, 11014=3분기
        fs_div: str = "CFS",            # CFS=연결, OFS=별도
    ) -> list[FinStatRow]:
        """단일 회사 전체 재무제표 (XBRL 기반)."""
        data = self._get_json(
            "fnlttSinglAcntAll.json",
            {
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": reprt_code,
                "fs_div": fs_div,
            },
        )
        if data.get("status") == DART_STATUS_NO_DATA:
            return []
        return [
            FinStatRow(
                corp_code=corp_code,
                bsns_year=bsns_year,
                reprt_code=reprt_code,
                fs_div=row.get("fs_div", fs_div),
                sj_div=row.get("sj_div", ""),
                account_nm=row.get("account_nm", ""),
                thstrm_amount=row.get("thstrm_amount", ""),
                frmtrm_amount=row.get("frmtrm_amount"),
                bfefrmtrm_amount=row.get("bfefrmtrm_amount"),
            )
            for row in data.get("list", [])
        ]
