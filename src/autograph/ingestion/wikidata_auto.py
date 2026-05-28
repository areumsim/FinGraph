"""Wikidata SPARQL — 자동차 제조사/모델/부품사 마스터 + Cross-Domain 매핑 키 수집.

목적:
- AutoGraph manufacturer/model 의 정식 명칭·국가·QID 확보.
- FinGraph corp_code 와 매핑 가능한 외부 식별자 (LEI, ISIN, P3320 한국사업자번호 등) 동시 적재.

SPARQL 쿼리 (3종):
1) manufacturers : ?mfr wdt:P31 wd:Q786820 (자동차 제조 회사).
   추가: 한국·미국·일본·독일 등 주요국 한정 (P17).
2) models        : ?model wdt:P31/wdt:P279* wd:Q3231690 (자동차 모델).
3) suppliers     : ?supplier wdt:P31/wdt:P279* wd:Q1259897 (자동차 부품 제조사).

저장 (멱등):
    data/raw/auto/wikidata/manufacturers.jsonl
    data/raw/auto/wikidata/models.jsonl
    data/raw/auto/wikidata/suppliers.jsonl

CLI:
    python -m autograph.ingestion.wikidata_auto --kind manufacturers
    python -m autograph.ingestion.wikidata_auto --all
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

import httpx

from autonexusgraph.ingestion._common import (
    CheckpointStore,
    RateLimiter,
    fetch_with_retry,
    raw_dir,
    save_raw,
)
from ..config import get_auto_settings


log = logging.getLogger(__name__)

_LIMITER = RateLimiter(per_sec=1.0)        # SPARQL 보수적
_SOURCE = "auto/wikidata"


# ── SPARQL 쿼리 ────────────────────────────────────────────────
SPARQL_MANUFACTURERS = """
SELECT ?mfr ?mfrLabel ?country ?countryLabel ?lei ?biznoKR WHERE {
  ?mfr wdt:P31/wdt:P279* wd:Q786820 .
  OPTIONAL { ?mfr wdt:P17 ?country . }
  OPTIONAL { ?mfr wdt:P1278 ?lei . }
  OPTIONAL { ?mfr wdt:P3320 ?biznoKR . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "ko,en". }
}
"""

SPARQL_MODELS = """
SELECT ?model ?modelLabel ?mfr ?mfrLabel ?countryLabel ?inception WHERE {
  ?model wdt:P31/wdt:P279* wd:Q3231690 .
  OPTIONAL { ?model wdt:P176 ?mfr . }
  OPTIONAL { ?model wdt:P495 ?country . }
  OPTIONAL { ?model wdt:P571 ?inception . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "ko,en". }
}
LIMIT 8000
"""

SPARQL_SUPPLIERS = """
SELECT ?supplier ?supplierLabel ?countryLabel ?lei ?biznoKR WHERE {
  { ?supplier wdt:P31/wdt:P279* wd:Q1259897 . }
  UNION
  { ?supplier wdt:P452 wd:Q190117 . }     # 자동차 부품 산업
  OPTIONAL { ?supplier wdt:P17 ?country . }
  OPTIONAL { ?supplier wdt:P1278 ?lei . }
  OPTIONAL { ?supplier wdt:P3320 ?biznoKR . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "ko,en". }
}
LIMIT 5000
"""

# 자동차 부품 → 공급사 (P176 "manufactured by") 매핑. Wikidata 의 part-supplier
# 트리는 희소하지만 deterministic A/B 출처라 seed 가치가 있다. 결과는 staging_relations
# 에 candidate 로 적재 → P4 cross_validate 가 Neo4j 로 promote.
SPARQL_PART_SUPPLIES = """
SELECT DISTINCT ?part ?partLabel ?supplier ?supplierLabel ?countryLabel WHERE {
  ?part wdt:P31/wdt:P279* ?cls .
  VALUES ?cls {
    wd:Q1183344    # vehicle part
    wd:Q3454322    # automobile part / motor vehicle component
    wd:Q44539      # internal combustion engine
    wd:Q189075     # transmission
    wd:Q12888      # battery
    wd:Q193039     # tire
    wd:Q187588     # brake
    wd:Q1267283    # airbag
    wd:Q191768     # alternator
    wd:Q23905      # spark plug
  }
  ?part wdt:P176 ?supplier .
  OPTIONAL { ?supplier wdt:P17 ?country . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "ko,en". }
}
LIMIT 5000
"""

QUERIES = {
    "manufacturers": SPARQL_MANUFACTURERS,
    "models":        SPARQL_MODELS,
    "suppliers":     SPARQL_SUPPLIERS,
    "part_supplies": SPARQL_PART_SUPPLIES,
}


def _run_sparql(query: str) -> list[dict]:
    settings = get_auto_settings()
    headers = {
        "User-Agent": settings.wikidata_user_agent,
        "Accept": "application/sparql-results+json",
    }
    params = {"query": query, "format": "json"}

    def _do() -> list[dict]:
        with httpx.Client(timeout=120.0, headers=headers) as client:
            r = client.get(settings.wikidata_sparql_url, params=params)
            r.raise_for_status()
            return r.json().get("results", {}).get("bindings", [])

    _LIMITER.acquire()
    return fetch_with_retry(_do, max_tries=3, base=3.0)


def _binding_to_row(b: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in b.items():
        out[k] = v.get("value")
    # 'http://www.wikidata.org/entity/Q12345' → 'Q12345'
    for k in list(out.keys()):
        if isinstance(out[k], str) and out[k].startswith("http://www.wikidata.org/entity/"):
            out[k + "_qid"] = out[k].rsplit("/", 1)[-1]
    return out


def ingest_kind(kind: str) -> dict:
    if kind not in QUERIES:
        raise ValueError(f"unknown kind: {kind!r}")

    ckpt = CheckpointStore(_SOURCE)
    if ckpt.is_done(kind):
        log.info("[wikidata] %s already done (delete state to re-run)", kind)
        return {"skipped": True}

    try:
        bindings = _run_sparql(QUERIES[kind])
        # JSONL append (멱등을 위해 일단 trunc 후 write)
        target = raw_dir(_SOURCE) / f"{kind}.jsonl"
        with target.open("w", encoding="utf-8") as f:
            for b in bindings:
                row = _binding_to_row(b)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        # raw 전체도 한 번 더 보존 (감사용)
        save_raw(_SOURCE, f"{kind}.raw.json", bindings)
        log.info("[wikidata] %s -> %d rows", kind, len(bindings))
        ckpt.mark_done(kind, {"rows": len(bindings)})
        return {"kind": kind, "rows": len(bindings)}
    except Exception as e:  # noqa: BLE001
        log.exception("[wikidata] failed %s", kind)
        ckpt.mark_failed(kind, str(e))
        return {"error": str(e)}


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.wikidata_auto")
    ap.add_argument("--kind", choices=sorted(QUERIES.keys()))
    ap.add_argument("--all", action="store_true", help="3종 전부")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    kinds = list(QUERIES.keys()) if args.all else ([args.kind] if args.kind else [])
    if not kinds:
        ap.error("--kind 또는 --all 필요")

    for k in kinds:
        ingest_kind(k)


if __name__ == "__main__":
    main()
