"""SEC EDGAR 클라이언트 — 한국 ADR 발행 회사의 SEC 공시.

키 불필요. SEC 정책상 User-Agent 에 contact 명시 필수.
무료 rate limit: 10 req/sec.

API:
- submissions:    https://data.sec.gov/submissions/CIK<10>.json
- companyfacts:   https://data.sec.gov/api/xbrl/companyfacts/CIK<10>.json
- companysearch:  https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<>&type=&dateb=&owner=include&count=40

한국 ADR 주요 발행사 CIK 예시 (Wikidata P5531 결과):
- Samsung Electronics:  미발행 (GDR/외국법인 등록만)
- KB Financial Group:   1325258
- SK Telecom:           1003415
- KEPCO:                1064306
- POSCO:                909308
- Korean Air:           해당 없음
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


SEC_BASE_DATA = "https://data.sec.gov"
SEC_BASE_WWW  = "https://www.sec.gov"


@dataclass(frozen=True)
class SecFiling:
    accession_no: str
    cik: str
    form_type: str
    filed_at: str        # YYYY-MM-DD
    period_of_report: str | None
    primary_doc_url: str


class SecEdgarClient:
    """SEC EDGAR Data API."""

    def __init__(self, user_agent: str, timeout: float = 30.0) -> None:
        if "@" not in user_agent:
            raise ValueError("SEC 정책상 User-Agent 에 contact email 필수.")
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )

    def __enter__(self) -> "SecEdgarClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self._client.close()

    def get_submissions(self, cik: str) -> dict | None:
        """회사 전체 공시 목록 (filings.recent 가 최근 1,000건)."""
        cik10 = str(cik).zfill(10)
        url = f"{SEC_BASE_DATA}/submissions/CIK{cik10}.json"
        resp = self._client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_company_facts(self, cik: str) -> dict | None:
        """XBRL companyfacts — 모든 GAAP/IFRS facts."""
        cik10 = str(cik).zfill(10)
        url = f"{SEC_BASE_DATA}/api/xbrl/companyfacts/CIK{cik10}.json"
        resp = self._client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def extract_filings(self, submissions: dict) -> list[SecFiling]:
        """submissions JSON 의 filings.recent → SecFiling list."""
        out: list[SecFiling] = []
        recent = submissions.get("filings", {}).get("recent", {})
        if not recent:
            return out
        n = len(recent.get("accessionNumber", []))
        cik = str(submissions.get("cik", "")).zfill(10)
        for i in range(n):
            acc = recent["accessionNumber"][i]
            form = recent.get("form", [""] * n)[i]
            filed = recent.get("filingDate", [""] * n)[i]
            period = recent.get("reportDate", [""] * n)[i]
            doc = recent.get("primaryDocument", [""] * n)[i]
            acc_clean = acc.replace("-", "")
            out.append(SecFiling(
                accession_no=acc,
                cik=cik,
                form_type=form,
                filed_at=filed,
                period_of_report=period or None,
                primary_doc_url=f"{SEC_BASE_WWW}/Archives/edgar/data/"
                                f"{int(cik)}/{acc_clean}/{doc}",
            ))
        return out
