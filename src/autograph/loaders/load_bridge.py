"""bridge.corp_entity 적재 — FinGraph corp_code ↔ AutoGraph entity 매핑.

매칭 전략 (순차, 각 단계는 confidence 차등):
1) wikidata_qid 매칭   — master.entity_map(id_type='wikidata_qid') ↔
                          auto.master_manufacturers.wikidata_qid
                          confidence = 1.000, reviewed_status = 'reviewed'
2) business_no 매칭    — master.companies.extra->>'bizr_no' / 'jurir_no' ↔
                          (wikidata facts 의 P3320) — confidence 0.95, candidate
3) name 정규화 매칭    — normalize_corp_name() 결과 1:1 → confidence 0.80, candidate
4) name fuzzy (substr) — confidence 0.60, candidate
5) wikidata suppliers  — 별도 entity_type='supplier' 후보, confidence 0.55, candidate

LLM 사용 금지. 룰 기반만.

CLI:
    python -m autograph.loaders.load_bridge
    python -m autograph.loaders.load_bridge --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name


def _ensure_supplier(cur, *, name: str, wikidata_qid: str | None,
                     country: str | None, lei: str | None,
                     business_no: str | None,
                     source: str, source_ref: str | None,
                     confidence: float) -> int:
    """auto.master_suppliers UPSERT — supplier_id 발급.

    매칭 우선순위: (1) wikidata_qid 정확, (2) name_norm 정확.
    중복 row 생성을 막아 :Supplier 노드가 Wikidata QID 별로 1개씩만 존재하게 한다.
    """
    name_norm = normalize_corp_name(name)
    # 1) QID 매칭
    if wikidata_qid:
        cur.execute("""
            SELECT supplier_id FROM auto.master_suppliers
             WHERE wikidata_qid = %s LIMIT 1
        """, (wikidata_qid,))
        r = cur.fetchone()
        if r:
            # 메타 보강 (NULL 만)
            cur.execute("""
                UPDATE auto.master_suppliers
                   SET name = COALESCE(name, %s),
                       name_norm = COALESCE(name_norm, %s),
                       country = COALESCE(country, %s),
                       lei = COALESCE(lei, %s),
                       business_no = COALESCE(business_no, %s),
                       updated_at = now()
                 WHERE supplier_id = %s
            """, (name, name_norm, country, lei, business_no, r[0]))
            return r[0]
    # 2) name_norm 매칭 (QID 없는 source 용)
    cur.execute("""
        SELECT supplier_id FROM auto.master_suppliers
         WHERE name_norm = %s LIMIT 1
    """, (name_norm,))
    r = cur.fetchone()
    if r:
        return r[0]
    # 3) 신규 INSERT
    cur.execute("""
        INSERT INTO auto.master_suppliers
          (name, name_norm, country, wikidata_qid, lei, business_no,
           source, source_ref, confidence, validated_status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'candidate')
        RETURNING supplier_id
    """, (name, name_norm, country, wikidata_qid, lei, business_no,
          source, source_ref, confidence))
    return cur.fetchone()[0]


log = logging.getLogger(__name__)


def _upsert_bridge(cur, *,
                   corp_code: str | None,
                   entity_id: str,
                   entity_type: str,
                   name: str | None,
                   wikidata_qid: str | None,
                   business_no: str | None,
                   match_method: str,
                   confidence_score: float,
                   reviewed_status: str = "candidate") -> None:
    cur.execute("""
        INSERT INTO bridge.corp_entity
          (corp_code, entity_id, entity_type, name, wikidata_qid, business_no,
           match_method, confidence_score, reviewed_status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (COALESCE(corp_code, ''), entity_type, entity_id) DO UPDATE SET
          name = COALESCE(EXCLUDED.name, bridge.corp_entity.name),
          wikidata_qid = COALESCE(EXCLUDED.wikidata_qid, bridge.corp_entity.wikidata_qid),
          business_no = COALESCE(EXCLUDED.business_no, bridge.corp_entity.business_no),
          confidence_score = GREATEST(bridge.corp_entity.confidence_score,
                                       EXCLUDED.confidence_score),
          updated_at = now()
    """, (corp_code, entity_id, entity_type, name, wikidata_qid, business_no,
          match_method, confidence_score, reviewed_status))


def match_manufacturers_by_qid(cur) -> int:
    """매칭 1단계 — Wikidata QID 정확 매치."""
    cur.execute("""
        SELECT em.corp_code, mm.manufacturer_id, mm.name, mm.wikidata_qid
          FROM auto.master_manufacturers mm
          JOIN master.entity_map em ON em.id_type = 'wikidata_qid'
                                   AND em.id_value = mm.wikidata_qid
         WHERE mm.wikidata_qid IS NOT NULL
    """)
    n = 0
    for corp_code, mfr_id, name, qid in cur.fetchall():
        _upsert_bridge(cur,
            corp_code=corp_code,
            entity_id=str(mfr_id),
            entity_type="manufacturer",
            name=name, wikidata_qid=qid,
            business_no=None,
            match_method="wikidata_qid",
            confidence_score=1.000,
            reviewed_status="reviewed")
        n += 1
    return n


def match_manufacturers_by_name(cur) -> int:
    """매칭 3단계 — name_norm exact (master.companies.corp_name 정규화 vs auto manufacturer name_norm)."""
    cur.execute("""
        SELECT c.corp_code, c.corp_name, mm.manufacturer_id, mm.name
          FROM auto.master_manufacturers mm
          JOIN master.companies c ON LOWER(REGEXP_REPLACE(c.corp_name,
                                            '\\(주\\)|㈜|주식회사|Inc\\.?|Ltd\\.?|Corp\\.?',
                                            '', 'g'))
                                  = mm.name_norm
    """)
    n = 0
    for corp_code, corp_name, mfr_id, name in cur.fetchall():
        _upsert_bridge(cur,
            corp_code=corp_code,
            entity_id=str(mfr_id),
            entity_type="manufacturer",
            name=name, wikidata_qid=None,
            business_no=None,
            match_method="name_exact",
            confidence_score=0.80,
            reviewed_status="candidate")
        n += 1
    return n


def match_suppliers_from_wikidata(cur) -> int:
    """매칭 5단계 — Wikidata suppliers.jsonl 의 후보 supplier 들을 등록.

    동작:
      1) auto.master_suppliers UPSERT → supplier_id 발급
      2) bridge.corp_entity 에는 entity_id = str(supplier_id) (Wikidata QID 는 wikidata_qid 컬럼)
         → manufacturer 행과 동일한 stringified-int 식별 체계로 통일.

    corp_code 매칭 시도 (LEI / business_no / name 정규화). 매칭 실패면 corp_code=NULL.
    """
    src = get_settings().ingest_raw_dir / "auto" / "wikidata" / "suppliers.jsonl"
    if not src.exists():
        log.info("[bridge] suppliers.jsonl 없음 — skip")
        return 0
    n = 0
    with src.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            qid = row.get("supplier_qid")
            name = row.get("supplierLabel")
            if not (qid and name):
                continue
            lei = row.get("lei")
            bizno = row.get("biznoKR")
            country = row.get("countryLabel")
            # Wikidata P3320 (사업자등록번호) 일부 row 가 URL/QID 등으로 오염됨 — 한국
            # 사업자번호 형식 (12자 이하, 숫자/하이픈) 가 아닌 값은 무시.
            if bizno and (len(bizno) > 40 or bizno.startswith("http")):
                bizno = None
            if lei and len(lei) > 20:
                lei = None

            # FinGraph 측과의 corp_code 매핑 — 가능한 단서 순으로 시도.
            corp_code = None
            method = "name_exact"
            conf = 0.55

            if lei:
                cur.execute("""
                    SELECT corp_code FROM master.entity_map
                     WHERE id_type='lei' AND id_value = %s LIMIT 1
                """, (lei,))
                r = cur.fetchone()
                if r:
                    corp_code, method, conf = r[0], "lei", 0.95
            if corp_code is None and bizno:
                cur.execute("""
                    SELECT corp_code FROM master.companies
                     WHERE extra->>'bizr_no' = %s
                        OR extra->>'jurir_no' = %s
                     LIMIT 1
                """, (bizno, bizno))
                r = cur.fetchone()
                if r:
                    corp_code, method, conf = r[0], "business_no", 0.90
            if corp_code is None:
                nn = normalize_corp_name(name)
                cur.execute("""
                    SELECT corp_code FROM master.companies
                     WHERE LOWER(REGEXP_REPLACE(corp_name,
                            '\\(주\\)|㈜|주식회사|Inc\\.?|Ltd\\.?|Corp\\.?',
                            '', 'g')) = %s
                     LIMIT 1
                """, (nn,))
                r = cur.fetchone()
                if r:
                    corp_code, method, conf = r[0], "name_exact", 0.80

            # auto.master_suppliers 에 등록 → supplier_id 발급.
            supplier_id = _ensure_supplier(cur,
                name=name, wikidata_qid=qid, country=country,
                lei=lei, business_no=bizno,
                source="wikidata", source_ref=qid,
                confidence=max(conf, 0.80))

            # bridge.corp_entity 에 stringified supplier_id 로 등록.
            _upsert_bridge(cur,
                corp_code=corp_code,
                entity_id=str(supplier_id),
                entity_type="supplier",
                name=name,
                wikidata_qid=qid,
                business_no=bizno,
                match_method=method,
                confidence_score=conf,
                reviewed_status=("reviewed" if conf >= 0.95 else "candidate"))
            n += 1
    return n


def load_all(*, dry_run: bool = False) -> dict:
    conn = get_connection()
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        out["qid_matched"]      = match_manufacturers_by_qid(cur)
        out["name_matched"]     = match_manufacturers_by_name(cur)
        out["suppliers_loaded"] = match_suppliers_from_wikidata(cur)
    if dry_run:
        conn.rollback()
        log.info("[bridge] dry-run rolled back: %s", out)
    else:
        conn.commit()
        log.info("[bridge] commit %s", out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_bridge")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_all(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
