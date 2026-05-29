"""data/raw/auto/sec_oem/CIK*.json → auto.oem_financials_sec + bridge.corp_entity.

SEC EDGAR Company Facts JSON 구조:
    {
      "cik": 1318605,
      "entityName": "Tesla, Inc.",
      "facts": {
        "us-gaap": {
          "Revenues": {
            "label": "Revenues", "description": "...",
            "units": {
              "USD": [
                {"end": "2023-12-31", "val": 96773000000, "accn": "...",
                 "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2024-01-29"},
                ...
              ]
            }
          },
          "NetIncomeLoss": { ... },
          ...
        },
        "dei": { ... }
      }
    }

본 loader 가 추출하는 concept (PRD §10 의 cross-domain QA 에 필요한 정량):

    Revenues / RevenuesNetOfInterestExpense / RevenuesFromContractWithCustomer*  → 매출
    NetIncomeLoss                                                                 → 순이익
    OperatingIncomeLoss                                                           → 영업이익
    GrossProfit                                                                   → 매출총이익
    ResearchAndDevelopmentExpense                                                 → R&D 지출
    Assets / AssetsCurrent                                                        → 자산
    Liabilities                                                                   → 부채
    StockholdersEquity                                                            → 자본
    InventoryNet                                                                  → 재고 (생산·공급 신호)
    CommonStockSharesOutstanding (dei)                                            → 발행주식수
    EntityCommonStockSharesOutstanding (dei)                                      → 발행주식수 (대체)

이외 vehicle deliveries / production 같은 OEM-specific custom concept 은 taxonomy='tsla-...'
등으로 등장 — 별도 화이트리스트로 처리 (선택, 본 PR 스코프 아님).

bridge.corp_entity 매핑:
    - 시드 OEM_SEED 의 (CIK, company_name) → auto.master_manufacturers 의 name 매칭 시도.
    - 매칭 성공 → bridge.corp_entity row (entity_type='manufacturer', entity_id=mfr_id,
      sec_cik=CIK, match_method='sec_cik', confidence=1.0, reviewed_status='reviewed').
    - 매칭 실패 → manufacturer 가 PG 에 없으니 bridge row 생성 보류. 사용자가 vpic 적재 후 재실행.

CLI:
    python -m autograph.loaders.load_auto_oem_sec
    python -m autograph.loaders.load_auto_oem_sec --dry-run
    python -m autograph.loaders.load_auto_oem_sec --cik 1318605
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name


log = logging.getLogger(__name__)


_CONFIDENCE = 0.95   # PRD §3.5 A 등급 (SEC 공식 XBRL).


# SEC CIK → vPIC manufacturer_id 직접 매핑.
# vPIC 의 GetAllMakes 가 SEC entity_name 과 표기가 달라서 normalize 만으로는
# 못 잡는 케이스. region/branding 차이 (Stellantis N.V. vs "Stellantis North America")
# 또는 vPIC 에 holding 자체가 미등록 (GM 은 Chevrolet/GMC/Cadillac/Buick brand 로만 등록).
_SEC_CIK_TO_VPIC_MFR_ID: dict[str, int] = {
    "0001605484": 1000000138,   # Stellantis N.V. → vPIC "Stellantis North America"
}

# vPIC 에 holding 자체가 없어서 manual mfr 신규 발급 대상.
# {sec_cik: (name, country)}.
_SEC_MANUAL_MFR_SEED: dict[str, tuple[str, str]] = {
    "0001467858": ("GENERAL MOTORS COMPANY", "USA"),
}

# Tier1 부품사 — manufacturer 가 아니라 supplier 로 bridge.
# 본 loader 는 OEM facts 도 받고 supplier bridge 도 보강. supplier facts 는
# auto.oem_financials_sec 에 manufacturer_id=NULL 로 그대로 적재됨 (분석 측에서
# bridge 통해 supplier 와 join).
# {sec_cik: supplier_name}.
_SEC_TIER1_SUPPLIER_SEED: dict[str, str] = {
    "0001521332": "APTIV PLC",
    # Magna 는 CIK 0001019975 — raw 미수집. 추후 추가.
}

# us-gaap / ifrs-full 화이트리스트 — 본 PR 의 핵심 fact 11종.
# concept 명은 SEC taxonomy 표준. 동일 의미의 두 concept 가 있는 경우 둘 다 받음
# (Revenues vs RevenueFromContractWithCustomerExcludingAssessedTax) — 분석 측에서 선택.
_GAAP_CONCEPTS: tuple[str, ...] = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "NetIncomeLoss",
    "OperatingIncomeLoss",
    "GrossProfit",
    "ResearchAndDevelopmentExpense",
    "Assets",
    "AssetsCurrent",
    "Liabilities",
    "StockholdersEquity",
    "InventoryNet",
)
_DEI_CONCEPTS: tuple[str, ...] = (
    "EntityCommonStockSharesOutstanding",
)


@dataclass
class LoadStats:
    ciks_seen:                 int = 0
    ciks_with_facts:           int = 0
    bridge_rows_upserted:      int = 0
    bridge_rows_unmatched:     int = 0
    financial_rows_inserted:   int = 0
    financial_rows_updated:    int = 0
    errors: list[str]          = field(default_factory=list)


def _raw_root() -> Path:
    return get_settings().ingest_raw_dir / "auto" / "sec_oem"


def _iter_cik_files() -> Iterable[tuple[str, Path]]:
    root = _raw_root()
    if not root.exists():
        return
    for p in sorted(root.glob("CIK*.json")):
        # 'submissions/' 하위 디렉토리는 facts 아니므로 skip.
        if p.parent.name == "submissions":
            continue
        cik10 = p.stem.replace("CIK", "")
        if not cik10.isdigit():
            continue
        yield cik10, p


def _ensure_tier1_supplier(cur, *, name: str, cik10: str) -> int:
    """Tier1 supplier 발급 — auto.master_suppliers 에 없으면 신규.

    supplier_id 시퀀스 충돌 회피를 위해 MAX+1 패턴 사용.
    """
    from autonexusgraph.ingestion._common import normalize_corp_name as _ncn
    norm = _ncn(name)
    cur.execute("""
        SELECT supplier_id FROM auto.master_suppliers
         WHERE name_norm = %s LIMIT 1
    """, (norm,))
    r = cur.fetchone()
    if r:
        return int(r[0])

    cur.execute("""
        SELECT GREATEST(COALESCE(MAX(supplier_id), 0), 9000000) + 1
          FROM auto.master_suppliers
    """)
    new_id = int(cur.fetchone()[0])
    cur.execute("""
        INSERT INTO auto.master_suppliers
            (supplier_id, name, name_norm, country, source, source_ref,
             confidence, validated_status)
        VALUES (%s, %s, %s, NULL, 'manual_sec_cik', %s, 0.95, 'reviewed')
    """, (new_id, name, norm, f"CIK{cik10}"))
    log.info("[load:sec_oem] manual supplier 신규 발급: id=%d %s (CIK%s)",
             new_id, name, cik10)
    return new_id


def _upsert_bridge_supplier(cur, *, supplier_id: int, sec_cik: str,
                              entity_name: str | None) -> bool:
    """Tier1 supplier 의 bridge upsert — entity_type='supplier'."""
    cur.execute("""
        INSERT INTO bridge.corp_entity
          (corp_code, entity_id, entity_type, name, sec_cik,
           match_method, confidence_score, reviewed_status)
        VALUES (NULL, %s, 'supplier', %s, %s,
                'sec_cik', 1.000, 'reviewed')
        ON CONFLICT (COALESCE(corp_code, ''), entity_type, entity_id) DO UPDATE SET
          sec_cik           = COALESCE(EXCLUDED.sec_cik, bridge.corp_entity.sec_cik),
          name              = COALESCE(EXCLUDED.name, bridge.corp_entity.name),
          confidence_score  = GREATEST(bridge.corp_entity.confidence_score,
                                       EXCLUDED.confidence_score),
          updated_at        = now()
    """, (str(supplier_id), entity_name, sec_cik))
    return True


def _ensure_manual_manufacturer(cur, *, name: str, country: str | None,
                                  cik10: str) -> int:
    """vPIC 미등록 OEM (예: GM holding) 의 manual mfr 발급.

    이미 같은 name_norm 의 manual mfr 이 있으면 그것 반환. 없으면 신규.
    manufacturer_id 는 (MAX, 10^9) + 1 패턴 — vPIC 자동 발급 ID (1~10^6 와 region
    1000000000~) 와 충돌 안 함.
    """
    norm = normalize_corp_name(name)
    cur.execute("""
        SELECT manufacturer_id FROM auto.master_manufacturers
         WHERE name_norm = %s AND source = 'manual_sec_cik' LIMIT 1
    """, (norm,))
    r = cur.fetchone()
    if r:
        return int(r[0])

    cur.execute("""
        SELECT GREATEST(COALESCE(MAX(manufacturer_id), 0), 2000000000) + 1
          FROM auto.master_manufacturers
    """)
    new_id = int(cur.fetchone()[0])
    cur.execute("""
        INSERT INTO auto.master_manufacturers
            (manufacturer_id, name, name_norm, country, source, source_ref,
             confidence, validated_status)
        VALUES (%s, %s, %s, %s, 'manual_sec_cik', %s, 0.95, 'reviewed')
    """, (new_id, name, norm, country, f"CIK{cik10}"))
    log.info("[load:sec_oem] manual mfr 신규 발급: id=%d %s (CIK%s)",
             new_id, name, cik10)
    return new_id


def _resolve_manufacturer_id(cur, *, entity_name: str,
                              cik10: str) -> int | None:
    """SEC entity_name → auto.master_manufacturers.manufacturer_id 매칭.

    0) bridge 에 이미 매핑 있으면 사용.
    1) _SEC_CIK_TO_VPIC_MFR_ID alias 매핑.
    2) name_norm 정확 매칭.
    3) ", Inc." / ", Corporation" / " Motor Company" / " Motors" / " Corp" 등 trim 후 매칭.
    4) _SEC_MANUAL_MFR_SEED 에 있으면 manual mfr 발급.
    실패 → None.
    """
    # 0) bridge 에 이미 매핑 있나?
    cur.execute("""
        SELECT entity_id::bigint
          FROM bridge.corp_entity
         WHERE sec_cik = %s AND entity_type = 'manufacturer'
         LIMIT 1
    """, (cik10,))
    r = cur.fetchone()
    if r:
        return int(r[0])

    # 1) alias dict (region 분리, branding 차이 등).
    if cik10 in _SEC_CIK_TO_VPIC_MFR_ID:
        return _SEC_CIK_TO_VPIC_MFR_ID[cik10]

    if not entity_name:
        return None

    # 1) 직접 name_norm 매칭.
    norm = normalize_corp_name(entity_name)
    cur.execute("""
        SELECT manufacturer_id FROM auto.master_manufacturers
         WHERE name_norm = %s LIMIT 1
    """, (norm,))
    r = cur.fetchone()
    if r:
        return int(r[0])

    # 2) 흔한 접미사 trim 후 재시도.
    candidates = [entity_name]
    for suf in (", Inc.", " Inc.", ", Inc",
                " Corporation", ", Corp.", " Corp.", " Corp",
                " Motor Company", " Motor Co., Ltd.", " Motors",
                " N.V.", " plc", " PLC", " Holding", " Holdings",
                " Group", " International Inc.", " International"):
        if entity_name.endswith(suf):
            candidates.append(entity_name[: -len(suf)].strip())
    # 첫 단어만으로도 시도 (예: 'Tesla, Inc.' → 'Tesla').
    first = entity_name.split(",")[0].split()[0:1]
    if first:
        candidates.append(first[0])

    for cand in candidates:
        nc = normalize_corp_name(cand)
        if not nc:
            continue
        cur.execute("""
            SELECT manufacturer_id FROM auto.master_manufacturers
             WHERE name_norm = %s OR name_norm = %s
             LIMIT 1
        """, (nc, nc.upper()))
        r = cur.fetchone()
        if r:
            return int(r[0])

    # 4) vPIC 미등록 holding (예: GM) — manual mfr 신규 발급.
    if cik10 in _SEC_MANUAL_MFR_SEED:
        manual_name, manual_country = _SEC_MANUAL_MFR_SEED[cik10]
        return _ensure_manual_manufacturer(
            cur, name=manual_name, country=manual_country, cik10=cik10,
        )

    return None


def _upsert_bridge(cur, *, manufacturer_id: int, sec_cik: str,
                   entity_name: str | None) -> bool:
    """bridge.corp_entity row UPSERT — SEC CIK ↔ manufacturer_id 매핑.

    같은 (corp_code='', entity_type='manufacturer', entity_id) 가 이미 있으면
    sec_cik 컬럼만 보강. 없으면 신규.
    """
    cur.execute("""
        INSERT INTO bridge.corp_entity
          (corp_code, entity_id, entity_type, name, sec_cik,
           match_method, confidence_score, reviewed_status)
        VALUES (NULL, %s, 'manufacturer', %s, %s,
                'sec_cik', 1.000, 'reviewed')
        ON CONFLICT (COALESCE(corp_code, ''), entity_type, entity_id) DO UPDATE SET
          sec_cik           = COALESCE(EXCLUDED.sec_cik, bridge.corp_entity.sec_cik),
          name              = COALESCE(EXCLUDED.name, bridge.corp_entity.name),
          confidence_score  = GREATEST(bridge.corp_entity.confidence_score,
                                       EXCLUDED.confidence_score),
          updated_at        = now()
    """, (str(manufacturer_id), entity_name, sec_cik))
    return True


def _iter_facts(facts_root: dict, taxonomy: str,
                concept_whitelist: tuple[str, ...]) -> Iterable[dict]:
    """facts.<taxonomy>.<concept>.units.<unit> [{end, val, accn, fy, fp, form, filed, start?}, ...]

    yield: {taxonomy, concept, unit, end, val, accn, fy, fp, form, filed, start?}
    """
    block = (facts_root or {}).get(taxonomy)
    if not block:
        return
    for concept in concept_whitelist:
        entry = block.get(concept)
        if not entry:
            continue
        units = (entry.get("units") or {})
        for unit_name, facts in units.items():
            for f in facts or []:
                yield {
                    "taxonomy": taxonomy,
                    "concept": concept,
                    "unit": unit_name,
                    "end": f.get("end"),
                    "start": f.get("start"),
                    "val": f.get("val"),
                    "accn": f.get("accn"),
                    "fy": f.get("fy"),
                    "fp": f.get("fp"),
                    "form": f.get("form"),
                    "filed": f.get("filed"),
                }


def _process_cik_file(cur, path: Path, stats: LoadStats) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        stats.errors.append(f"{path.name} parse: {e}")
        return

    cik10 = str(data.get("cik") or path.stem.replace("CIK", "")).zfill(10)
    entity_name = data.get("entityName") or ""
    stats.ciks_seen += 1

    facts_root = data.get("facts") or {}
    if not facts_root:
        log.warning("[load:sec_oem] CIK%s — facts 비어있음", cik10)
        return
    stats.ciks_with_facts += 1

    # 1) manufacturer_id 매칭 + bridge upsert.
    mfr_id = _resolve_manufacturer_id(
        cur, entity_name=entity_name, cik10=cik10,
    )
    if mfr_id is not None:
        try:
            _upsert_bridge(cur, manufacturer_id=mfr_id,
                            sec_cik=cik10, entity_name=entity_name)
            stats.bridge_rows_upserted += 1
        except Exception as e:   # noqa: BLE001
            stats.errors.append(f"bridge {cik10}: {e}")
    elif cik10 in _SEC_TIER1_SUPPLIER_SEED:
        # Tier1 부품사 (예: Aptiv) — entity_type='supplier' 로 bridge.
        sup_name = _SEC_TIER1_SUPPLIER_SEED[cik10]
        try:
            sup_id = _ensure_tier1_supplier(cur, name=sup_name, cik10=cik10)
            _upsert_bridge_supplier(cur, supplier_id=sup_id,
                                     sec_cik=cik10, entity_name=entity_name)
            stats.bridge_rows_upserted += 1
        except Exception as e:   # noqa: BLE001
            stats.errors.append(f"bridge_supplier {cik10}: {e}")
    else:
        stats.bridge_rows_unmatched += 1
        log.warning("[load:sec_oem] CIK%s (%s) — manufacturer 매칭 실패. "
                    "vpic 적재 선행 필요.", cik10, entity_name)

    # 2) facts UPSERT — 화이트리스트 concept 만.
    all_facts = list(_iter_facts(facts_root, "us-gaap", _GAAP_CONCEPTS))
    all_facts += list(_iter_facts(facts_root, "dei", _DEI_CONCEPTS))

    for f in all_facts:
        val = f.get("val")
        if val is None:
            continue
        # 분기 facts (Q1/Q2/Q3) 도 받음. fy/fp 모두 None 인 row 는 skip.
        if f.get("fy") is None and f.get("fp") is None:
            continue
        cur.execute("SAVEPOINT sp_sec_fact")
        try:
            cur.execute("""
                INSERT INTO auto.oem_financials_sec
                  (manufacturer_id, sec_cik, taxonomy, concept, unit,
                   fiscal_year, fiscal_period, period_end, period_start,
                   value, form_type, accession_no, filed_at, raw)
                VALUES (%s, %s, %s, %s, %s,
                        %s, %s, NULLIF(%s, '')::date, NULLIF(%s, '')::date,
                        %s, %s, %s, NULLIF(%s, '')::date, %s::jsonb)
                ON CONFLICT (sec_cik, concept, fiscal_year, fiscal_period, unit, form_type)
                DO UPDATE SET
                  manufacturer_id = COALESCE(EXCLUDED.manufacturer_id,
                                              auto.oem_financials_sec.manufacturer_id),
                  value           = EXCLUDED.value,
                  period_end      = COALESCE(EXCLUDED.period_end,
                                              auto.oem_financials_sec.period_end),
                  period_start    = COALESCE(EXCLUDED.period_start,
                                              auto.oem_financials_sec.period_start),
                  accession_no    = EXCLUDED.accession_no,
                  filed_at        = COALESCE(EXCLUDED.filed_at,
                                              auto.oem_financials_sec.filed_at),
                  raw             = EXCLUDED.raw,
                  ingested_at     = now()
                RETURNING (xmax = 0) AS inserted
            """, (
                mfr_id, cik10, f["taxonomy"], f["concept"], f["unit"],
                f.get("fy"), f.get("fp"),
                f.get("end") or "", f.get("start") or "",
                val,
                f.get("form"), f.get("accn"),
                f.get("filed") or "",
                json.dumps({"end": f.get("end"), "start": f.get("start"),
                            "accn": f.get("accn"), "fp": f.get("fp"),
                            "form": f.get("form"), "filed": f.get("filed")},
                           ensure_ascii=False),
            ))
            inserted = cur.fetchone()[0]
            cur.execute("RELEASE SAVEPOINT sp_sec_fact")
            if inserted:
                stats.financial_rows_inserted += 1
            else:
                stats.financial_rows_updated += 1
        except Exception as e:   # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT sp_sec_fact")
            stats.errors.append(
                f"{cik10}/{f['concept']}/{f.get('fy')}/{f.get('fp')}: {e}"
            )


def load_oem_sec(*, dry_run: bool = False,
                 ciks: list[str] | None = None) -> LoadStats:
    stats = LoadStats()
    files = list(_iter_cik_files())
    if not files:
        log.warning("[load:sec_oem] raw 디렉토리 비어있음 — ingestion 먼저: %s",
                    _raw_root())
        return stats

    # 필터 (ciks 지정 시).
    cik_set: set[str] | None = None
    if ciks:
        cik_set = {str(c).zfill(10) for c in ciks}

    conn = get_connection()
    with conn.cursor() as cur:
        for cik10, path in files:
            if cik_set and cik10 not in cik_set:
                continue
            _process_cik_file(cur, path, stats)

    if dry_run:
        conn.rollback()
        log.info(
            "[load:sec_oem] DRY-RUN ciks=%d with_facts=%d "
            "bridge_upserted=%d bridge_unmatched=%d "
            "would_ins=%d would_upd=%d errors=%d",
            stats.ciks_seen, stats.ciks_with_facts,
            stats.bridge_rows_upserted, stats.bridge_rows_unmatched,
            stats.financial_rows_inserted, stats.financial_rows_updated,
            len(stats.errors),
        )
        return stats

    conn.commit()
    log.info(
        "[load:sec_oem] ciks=%d with_facts=%d "
        "bridge_upserted=%d bridge_unmatched=%d "
        "ins=%d upd=%d errors=%d",
        stats.ciks_seen, stats.ciks_with_facts,
        stats.bridge_rows_upserted, stats.bridge_rows_unmatched,
        stats.financial_rows_inserted, stats.financial_rows_updated,
        len(stats.errors),
    )
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_oem_sec")
    ap.add_argument("--cik", help="단일 CIK 만 (예: 1318605)")
    ap.add_argument("--ciks", help="콤마 구분")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    ciks: list[str] | None = None
    if args.cik:
        ciks = [args.cik]
    elif args.ciks:
        ciks = [c.strip() for c in args.ciks.split(",") if c.strip()]

    load_oem_sec(dry_run=args.dry_run, ciks=ciks)


if __name__ == "__main__":
    main()


__all__ = [
    "load_oem_sec", "LoadStats",
    "_iter_cik_files", "_resolve_manufacturer_id",
    "_iter_facts", "_upsert_bridge",
    "_GAAP_CONCEPTS", "_DEI_CONCEPTS", "_CONFIDENCE",
]
