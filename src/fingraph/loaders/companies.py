"""master.companies 적재 — DART corp_codes + company.json + KRX 시장/시총.

원본:
- data/raw/ingest_targets.jsonl (KRX 매칭 결과 — corp_code/stock_code/market/cap)
- data/raw/dart_bulk/corp/<corp_code>/company.json (DART 회사 개황)

PK: master.companies.corp_code
upsert: extras / 시장 / 시가총액 등 변경 가능 정보는 갱신.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import get_settings
from ._common import LoadStats, iter_jsonl, parse_date


SQL_UPSERT = """
INSERT INTO master.companies
  (corp_code, corp_name, stock_code, market, sector, industry, listed_at, is_active, extra, updated_at)
VALUES
  (%(corp_code)s, %(corp_name)s, %(stock_code)s, %(market)s, %(sector)s, %(industry)s,
   %(listed_at)s, TRUE, %(extra)s::jsonb, now())
ON CONFLICT (corp_code) DO UPDATE SET
  corp_name = EXCLUDED.corp_name,
  stock_code = EXCLUDED.stock_code,
  market = COALESCE(EXCLUDED.market, master.companies.market),
  sector = COALESCE(EXCLUDED.sector, master.companies.sector),
  industry = COALESCE(EXCLUDED.industry, master.companies.industry),
  listed_at = COALESCE(EXCLUDED.listed_at, master.companies.listed_at),
  extra = master.companies.extra || EXCLUDED.extra,
  updated_at = now()
"""


def _build_row(target: dict, company_path: Path) -> dict[str, Any] | None:
    """ingest_target + company.json → master.companies row dict."""
    corp_code = target.get("corp_code")
    if not corp_code:
        return None

    # company.json 이 있으면 풍부한 정보 사용, 없으면 target 만으로
    company: dict = {}
    if company_path.exists():
        try:
            company = json.loads(company_path.read_text(encoding="utf-8"))
        except Exception:
            company = {}

    # corp_name 우선순위: company.json > target.name_dart > target.name_krx
    corp_name = (
        company.get("corp_name")
        or target.get("name_dart")
        or target.get("name_krx")
        or "(unknown)"
    )

    # 시장: target.market (KRX 기준) > company.corp_cls 변환
    market = target.get("market")
    if not market:
        # Y=KOSPI, K=KOSDAQ, N=KONEX, E=ETC
        m = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX", "E": "OTHER"}.get(
            company.get("corp_cls", ""), None
        )
        market = m

    # 추가 메타 (JSONB)
    extra = {
        "name_krx": target.get("name_krx"),
        "market_cap_krw": target.get("market_cap"),
        "isin": target.get("isin"),
        "ceo_nm": company.get("ceo_nm"),
        "corp_cls": company.get("corp_cls"),
        "jurir_no": company.get("jurir_no"),       # 법인등록번호
        "bizr_no": company.get("bizr_no"),          # 사업자등록번호
        "adres": company.get("adres"),
        "hm_url": company.get("hm_url"),
        "phn_no": company.get("phn_no"),
        "fax_no": company.get("fax_no"),
        "ir_url": company.get("ir_url"),
        "acc_mt": company.get("acc_mt"),            # 결산월
        "est_dt": company.get("est_dt"),            # 설립일
    }
    # None 값 제거
    extra = {k: v for k, v in extra.items() if v is not None and v != ""}

    return {
        "corp_code": corp_code,
        "corp_name": corp_name,
        "stock_code": target.get("stock_code") or (company.get("stock_code") or None),
        "market": market,
        "sector": target.get("sector") or company.get("induty_code"),
        "industry": company.get("induty_code"),
        "listed_at": parse_date(company.get("est_dt")),    # 설립일을 listed_at 대용
        "extra": json.dumps(extra, ensure_ascii=False),
    }


def load_companies(
    *,
    targets_path: Path | None = None,
    bulk_root: Path | None = None,
    dry_run: bool = False,
    batch_size: int = 200,
) -> LoadStats:
    """ingest_targets.jsonl + 각 corp 의 company.json → master.companies upsert."""
    s = get_settings()
    targets_path = targets_path or (s.ingest_raw_dir / "ingest_targets.jsonl")
    bulk_root = bulk_root or (s.ingest_raw_dir / "dart_bulk" / "corp")

    if not targets_path.exists():
        raise FileNotFoundError(f"targets 없음: {targets_path}")

    stats = LoadStats()
    rows: list[dict] = []
    for target in iter_jsonl(targets_path):
        row = _build_row(target, bulk_root / target.get("corp_code", "") / "company.json")
        if row is None:
            stats.skipped += 1
            continue
        rows.append(row)

    if dry_run:
        stats.batches = (len(rows) + batch_size - 1) // batch_size
        stats.inserted = len(rows)
        if rows:
            stats.sql_preview.append(SQL_UPSERT.strip())
            stats.sql_preview.append(f"-- sample params: {json.dumps(rows[0], ensure_ascii=False)[:300]}...")
        return stats

    # 실제 적재
    from ..db.postgres import transaction
    with transaction() as conn:
        with conn.cursor() as cur:
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                try:
                    cur.executemany(SQL_UPSERT, batch)
                    # executemany 는 inserted/updated 구분 안 됨 — 합산만
                    stats.inserted += len(batch)
                    stats.batches += 1
                except Exception:
                    stats.failed += len(batch)
                    raise
    return stats
