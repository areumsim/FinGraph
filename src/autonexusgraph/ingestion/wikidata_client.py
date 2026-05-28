"""Wikidata SPARQL 클라이언트 — 한국 상장사 QID 매핑 + 부가 속성.

엔드포인트: https://query.wikidata.org/sparql (rate limit 보수적)
라이선스: CC0 — 자유 사용

핵심 사용처:
1. corp_code 또는 회사명/사업자번호 ↔ Wikidata QID 매핑 (Entity Resolution 핵심)
2. 글로벌 ID 매핑: ISIN, LEI, CIK, X (Twitter), 공식 웹사이트
3. 부가 속성: 설립일, 본사, 산업, CEO(P169), 자회사(P355), 모회사(P749)

전략:
- 한 번의 SPARQL 쿼리로 한국 상장사 후보군을 받아오고,
  client 측에서 회사명·종목코드·jurir_no 매칭으로 corp_code 와 묶는다.
- 회사별 상세 속성은 별도 쿼리 (또는 wbgetentities API)

호출 패턴:
    from autonexusgraph.ingestion.wikidata_client import WikidataClient
    with WikidataClient() as wd:
        candidates = wd.fetch_korean_listed_companies()
        # 또는
        details = wd.fetch_entity('Q35476')
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx


SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
ENTITY_ENDPOINT = "https://www.wikidata.org/wiki/Special:EntityData"

USER_AGENT = "FinGraph/0.1 (research; https://github.com/areumsim/FinGraph)"


@dataclass(frozen=True)
class WikidataCandidate:
    """SPARQL 으로 받은 한국 상장사 후보 1건."""
    qid: str
    label_ko: str | None
    label_en: str | None
    ticker: str | None         # KRX 종목코드 (P414 에 시가총액 stmt 또는 P249)
    isin: str | None
    lei: str | None
    cik: str | None
    homepage: str | None
    instance_of: list[str]     # P31 값들 (회사·corporation 등)
    inception: str | None      # 설립일
    headquarters: str | None
    industry: str | None


# 한국 상장사 후보를 폭넓게 가져오는 SPARQL.
# - 거래소(P414) 가 KRX(Q33685) 또는 KOSPI(Q189094)/KOSDAQ(Q488556) 이거나
# - 국적(P17) 이 대한민국(Q884) 이고 instance of business(Q4830453)/corporation(Q167037)/...
# label·optional 속성은 left join.
_SPARQL_KOREAN_COMPANIES = """
SELECT DISTINCT ?company ?companyLabel ?companyLabelEn
                ?ticker ?isin ?lei ?cik ?homepage
                ?inception ?hqLabel ?industryLabel
WHERE {
  {
    ?company wdt:P414 wd:Q33685 .   # listed on KRX
  } UNION {
    ?company wdt:P414 wd:Q189094 .  # KOSPI
  } UNION {
    ?company wdt:P414 wd:Q488556 .  # KOSDAQ
  } UNION {
    ?company wdt:P17 wd:Q884 ;
             wdt:P31/wdt:P279* wd:Q4830453 .   # business
  }
  OPTIONAL { ?company wdt:P249  ?ticker . }
  OPTIONAL { ?company wdt:P946  ?isin . }
  OPTIONAL { ?company wdt:P1278 ?lei . }
  OPTIONAL { ?company wdt:P5531 ?cik . }
  OPTIONAL { ?company wdt:P856  ?homepage . }
  OPTIONAL { ?company wdt:P571  ?inception . }
  OPTIONAL { ?company wdt:P159  ?hq . }
  OPTIONAL { ?company wdt:P452  ?industry . }
  SERVICE wikibase:label {
    bd:serviceParam wikibase:language "ko,en" .
    ?company rdfs:label ?companyLabel .
    ?hq      rdfs:label ?hqLabel .
    ?industry rdfs:label ?industryLabel .
  }
  SERVICE wikibase:label {
    bd:serviceParam wikibase:language "en" .
    ?company rdfs:label ?companyLabelEn .
  }
}
LIMIT 5000
"""


class WikidataClient:
    """Wikidata SPARQL/Entity API 클라이언트."""

    def __init__(self, timeout: float = 90.0) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/sparql-results+json",
            },
        )

    def __enter__(self) -> "WikidataClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── SPARQL ────────────────────────────────────────────────
    def sparql(self, query: str) -> dict:
        """SPARQL 결과 JSON."""
        resp = self._client.get(SPARQL_ENDPOINT, params={"query": query, "format": "json"})
        resp.raise_for_status()
        return resp.json()

    def fetch_korean_listed_companies(self) -> list[WikidataCandidate]:
        """한국 상장사 후보 일괄 — 5,000건 한도. 본 시스템 300개 매칭에 충분."""
        raw = self.sparql(_SPARQL_KOREAN_COMPANIES)
        out: list[WikidataCandidate] = []
        seen: set[str] = set()
        for b in raw.get("results", {}).get("bindings", []):
            uri = b.get("company", {}).get("value", "")
            qid = uri.rsplit("/", 1)[-1] if uri else ""
            if not qid or qid in seen:
                continue
            seen.add(qid)
            out.append(WikidataCandidate(
                qid=qid,
                label_ko=_v(b, "companyLabel"),
                label_en=_v(b, "companyLabelEn"),
                ticker=_v(b, "ticker"),
                isin=_v(b, "isin"),
                lei=_v(b, "lei"),
                cik=_v(b, "cik"),
                homepage=_v(b, "homepage"),
                inception=_v(b, "inception"),
                headquarters=_v(b, "hqLabel"),
                industry=_v(b, "industryLabel"),
                instance_of=[],
            ))
        return out

    # ── Entity API (개별 회사 상세) ────────────────────────────
    def fetch_entity(self, qid: str) -> dict | None:
        """단일 entity 의 전체 statement (P31 / P169 / P355 / P127 / ... 포함).

        반환: 원본 JSON (entities.<qid>) — load_wikidata 가 property 별로 파싱.
        """
        url = f"{ENTITY_ENDPOINT}/{qid}.json"
        resp = self._client.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data.get("entities", {}).get(qid)


def _v(b: dict, key: str) -> str | None:
    """SPARQL binding 의 value 안전 추출."""
    v = b.get(key)
    if isinstance(v, dict):
        val = v.get("value")
        return val if val else None
    return None


# ── property 파싱 헬퍼 (load_wikidata 에서 사용) ─────────────
def claim_values(entity: dict, property_id: str) -> list[dict]:
    """entity.claims[property_id] → list of mainsnak.datavalue.value (dict)."""
    claims = entity.get("claims", {}).get(property_id, [])
    out: list[dict] = []
    for c in claims:
        snak = c.get("mainsnak", {})
        if snak.get("snaktype") != "value":
            continue
        dv = snak.get("datavalue", {})
        v = dv.get("value")
        if v is not None:
            out.append({"value": v, "type": dv.get("type")})
    return out


def claim_string_values(entity: dict, property_id: str) -> list[str]:
    """claim_values 중 type=string/external-id 의 raw 문자열 list."""
    out: list[str] = []
    for c in claim_values(entity, property_id):
        v = c["value"]
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict) and "id" in v:
            out.append(v["id"])
    return out


def claim_qid_values(entity: dict, property_id: str) -> list[str]:
    """claim_values 중 wikibase-item 의 QID 값 list."""
    out: list[str] = []
    for c in claim_values(entity, property_id):
        v = c["value"]
        if isinstance(v, dict) and "id" in v:
            out.append(v["id"])
    return out
