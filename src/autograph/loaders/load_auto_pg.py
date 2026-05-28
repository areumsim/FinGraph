"""raw → PostgreSQL (auto.*) 멱등 적재.

읽는 raw:
    data/raw/auto/nhtsa_vpic/all_makes.json
    data/raw/auto/nhtsa_vpic/{MAKE}/{YEAR}/variants.jsonl
    data/raw/auto/nhtsa_recalls/{MAKE}/{MODEL}/{YEAR}.json
    data/raw/auto/nhtsa_complaints/{MAKE}/{MODEL}/{YEAR}.json
    data/raw/auto/wikidata/{manufacturers|models|suppliers}.jsonl

적재 대상:
    auto.master_manufacturers
    auto.master_vehicle_models
    auto.master_vehicle_variants
    auto.events_recalls
    auto.events_complaints
    (auto.spec_measurements 은 vPIC variants 의 일부 측정값 + Canadian specs 에서 추출.
     MVP 에서는 빈 INSERT 만 — 단위·키 매핑은 후속.)

UPSERT 규칙:
    - manufacturers : (manufacturer_id) — NHTSA MakeId 우선.
    - models        : UNIQUE(manufacturer_id, name_norm, market)
    - variants      : UNIQUE(model_id, model_year, COALESCE(trim,''), COALESCE(fuel_type,''))
    - recalls       : UNIQUE(source, source_recall_no)
    - complaints    : UNIQUE(source, source_complaint_no)

CLI:
    python -m autograph.loaders.load_auto_pg --source all
    python -m autograph.loaders.load_auto_pg --source nhtsa_vpic
    python -m autograph.loaders.load_auto_pg --source nhtsa_recalls --dry-run
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


@dataclass
class LoadStats:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ── helpers ──────────────────────────────────────────────────
def _auto_raw_root() -> Path:
    return get_settings().ingest_raw_dir / "auto"


def _resolve_make_model_variant(
    cur: Any, make_name: str, model_name: str, model_year: int | None,
) -> tuple[int | None, int | None, int | None]:
    """make+model+year → (manufacturer_id, model_id, variant_id) 단일 LEFT JOIN 1회.

    이전: row 마다 mfr/model/variant 3 회 SELECT (N+1 패턴, 대량 적재 시 시간↑).
    현재: 한 쿼리로 모두. 없으면 해당 슬롯이 NULL.
    """
    if not make_name:
        return None, None, None
    cur.execute("""
        SELECT mm.manufacturer_id, m.model_id, v.variant_id
          FROM auto.master_manufacturers mm
          LEFT JOIN auto.master_vehicle_models m
            ON m.manufacturer_id = mm.manufacturer_id
           AND m.name_norm = %s
          LEFT JOIN auto.master_vehicle_variants v
            ON v.model_id = m.model_id
           AND v.model_year = %s::int
         WHERE mm.name_norm = %s
         LIMIT 1
    """, (
        normalize_corp_name(model_name) if model_name else None,
        model_year,
        normalize_corp_name(make_name),
    ))
    row = cur.fetchone()
    if not row:
        return None, None, None
    return row[0], row[1], row[2]


def _iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("[load] bad json line in %s: %s", path, e)
                continue


def _ensure_manufacturer(cur, *, name: str, source: str, source_ref: str | None,
                         country: str | None = None,
                         wikidata_qid: str | None = None,
                         manufacturer_id: int | None = None) -> int:
    """제조사 UPSERT. name + source 기준. manufacturer_id 우선 사용.

    Returns: manufacturer_id (BIGINT).
    """
    name_norm = normalize_corp_name(name)
    if manufacturer_id is not None:
        cur.execute("""
            INSERT INTO auto.master_manufacturers
              (manufacturer_id, name, name_norm, country, wikidata_qid,
               source, source_ref, raw)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (manufacturer_id) DO UPDATE SET
              name = EXCLUDED.name,
              name_norm = EXCLUDED.name_norm,
              country = COALESCE(EXCLUDED.country, auto.master_manufacturers.country),
              wikidata_qid = COALESCE(EXCLUDED.wikidata_qid, auto.master_manufacturers.wikidata_qid),
              updated_at = now()
            RETURNING manufacturer_id
        """, (manufacturer_id, name, name_norm, country, wikidata_qid,
              source, source_ref, json.dumps({}, ensure_ascii=False)))
        return cur.fetchone()[0]

    # manufacturer_id 미제공 — name_norm 으로 조회 후 자체 seq.
    cur.execute("""
        SELECT manufacturer_id FROM auto.master_manufacturers
        WHERE name_norm = %s LIMIT 1
    """, (name_norm,))
    r = cur.fetchone()
    if r:
        return r[0]
    # 자체 seq 영역 (>=10^9) — vPIC 와 겹치지 않게.
    cur.execute("""
        SELECT COALESCE(MAX(manufacturer_id), 999999999) + 1
          FROM auto.master_manufacturers
         WHERE manufacturer_id >= 1000000000
    """)
    new_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO auto.master_manufacturers
          (manufacturer_id, name, name_norm, country, wikidata_qid,
           source, source_ref, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING manufacturer_id
    """, (new_id, name, name_norm, country, wikidata_qid, source, source_ref,
          json.dumps({}, ensure_ascii=False)))
    return cur.fetchone()[0]


def _ensure_model(cur, *, manufacturer_id: int, name: str, market: str | None,
                  source: str, source_ref: str | None,
                  wikidata_qid: str | None = None) -> int:
    name_norm = normalize_corp_name(name)
    cur.execute("""
        INSERT INTO auto.master_vehicle_models
          (manufacturer_id, name, name_norm, market, wikidata_qid,
           source, source_ref, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (manufacturer_id, name_norm, market) DO UPDATE SET
          name = EXCLUDED.name,
          wikidata_qid = COALESCE(EXCLUDED.wikidata_qid,
                                  auto.master_vehicle_models.wikidata_qid),
          updated_at = now()
        RETURNING model_id
    """, (manufacturer_id, name, name_norm, market, wikidata_qid,
          source, source_ref, json.dumps({}, ensure_ascii=False)))
    return cur.fetchone()[0]


def _ensure_variant(cur, *, model_id: int, model_year: int,
                    trim: str | None = None,
                    fuel_type: str | None = None,
                    body_class: str | None = None,
                    source: str, source_ref: str | None,
                    raw: dict | None = None) -> int:
    """variants UPSERT — 부분 unique index 사용으로 COALESCE 정합."""
    cur.execute("""
        SELECT variant_id FROM auto.master_vehicle_variants
        WHERE model_id = %s AND model_year = %s
          AND COALESCE(trim, '') = COALESCE(%s, '')
          AND COALESCE(fuel_type, '') = COALESCE(%s, '')
        LIMIT 1
    """, (model_id, model_year, trim, fuel_type))
    r = cur.fetchone()
    if r:
        return r[0]
    cur.execute("""
        INSERT INTO auto.master_vehicle_variants
          (model_id, model_year, trim, fuel_type, body_class,
           source, source_ref, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING variant_id
    """, (model_id, model_year, trim, fuel_type, body_class,
          source, source_ref,
          json.dumps(raw or {}, ensure_ascii=False, default=str)))
    return cur.fetchone()[0]


# ── source loaders ───────────────────────────────────────────
def load_vpic(*, dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    root = _auto_raw_root() / "nhtsa_vpic"
    if not root.exists():
        log.warning("[load] vpic root missing: %s", root)
        return stats

    # 1) all_makes.json → manufacturers
    all_makes = root / "all_makes.json"
    conn = get_connection()
    with conn.cursor() as cur:
        if all_makes.exists():
            data = json.loads(all_makes.read_text(encoding="utf-8"))
            for m in data.get("Results") or []:
                cur.execute("SAVEPOINT sp_vpic_mfr")
                try:
                    _ensure_manufacturer(cur,
                        manufacturer_id=int(m["Make_ID"]),
                        name=m["Make_Name"],
                        source="nhtsa_vpic",
                        source_ref=str(m["Make_ID"]))
                    cur.execute("RELEASE SAVEPOINT sp_vpic_mfr")
                    stats.inserted += 1
                except Exception as e:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT sp_vpic_mfr")
                    stats.errors.append(f"vpic make {m.get('Make_ID')}: {e}")

        # 2) {make}/{year}/variants.jsonl → models + variants
        for variants_file in root.glob("*/*/variants.jsonl"):
            for row in _iter_jsonl(variants_file):
                cur.execute("SAVEPOINT sp_vpic_var")
                try:
                    make_id = int(row["make_id"])
                    make_name = row["make"]
                    model_name = row["model_name"]
                    model_year = int(row["model_year"])

                    # 제조사가 all_makes 에 없을 수도 있어 다시 보장
                    _ensure_manufacturer(cur,
                        manufacturer_id=make_id,
                        name=make_name,
                        source="nhtsa_vpic",
                        source_ref=str(make_id))
                    model_id = _ensure_model(cur,
                        manufacturer_id=make_id,
                        name=model_name,
                        market="US",
                        source="nhtsa_vpic",
                        source_ref=str(row.get("model_id_vpic")))
                    _ensure_variant(cur,
                        model_id=model_id,
                        model_year=model_year,
                        trim=None,
                        fuel_type=None,
                        source="nhtsa_vpic",
                        source_ref=f"{make_id}/{row.get('model_id_vpic')}/{model_year}",
                        raw=row.get("raw") or {})
                    cur.execute("RELEASE SAVEPOINT sp_vpic_var")
                    stats.inserted += 1
                except Exception as e:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT sp_vpic_var")
                    stats.errors.append(f"vpic variant {variants_file}: {e}")
    if dry_run:
        conn.rollback()
        log.info("[load:vpic] dry-run rolled back. would insert ~%d", stats.inserted)
    else:
        conn.commit()
        log.info("[load:vpic] commit inserted=%d errors=%d",
                 stats.inserted, len(stats.errors))
    return stats


def load_recalls(*, dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    root = _auto_raw_root() / "nhtsa_recalls"
    if not root.exists():
        log.warning("[load] recalls root missing: %s", root)
        return stats

    conn = get_connection()
    with conn.cursor() as cur:
        for f in root.glob("*/*/*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                stats.errors.append(f"recalls bad json {f}: {e}")
                continue

            for r in data.get("results") or []:
                # row 별 savepoint — 한 row 가 실패해도 트랜잭션 전체 abort 되지 않게.
                cur.execute("SAVEPOINT sp_recall")
                try:
                    make_name = r.get("Make") or ""
                    model_name = r.get("Model") or ""
                    model_year = int(r.get("ModelYear") or 0) or None

                    manufacturer_id, model_id, variant_id = _resolve_make_model_variant(
                        cur, make_name, model_name, model_year)

                    cur.execute("""
                        INSERT INTO auto.events_recalls
                          (source, source_recall_no, manufacturer_id, model_id, variant_id,
                           component_text, defect_summary, consequence, remedy_summary,
                           report_date, country, affected_units, raw, snapshot_year)
                        VALUES ('nhtsa', %s, %s, %s, %s, %s, %s, %s, %s,
                                NULLIF(%s, '')::date, %s,
                                NULLIF(%s, '')::bigint,
                                %s::jsonb, %s)
                        ON CONFLICT (source, source_recall_no) DO UPDATE SET
                          manufacturer_id = COALESCE(EXCLUDED.manufacturer_id,
                                                      auto.events_recalls.manufacturer_id),
                          model_id        = COALESCE(EXCLUDED.model_id,
                                                      auto.events_recalls.model_id),
                          variant_id      = COALESCE(EXCLUDED.variant_id,
                                                      auto.events_recalls.variant_id),
                          raw             = EXCLUDED.raw,
                          ingested_at     = now()
                    """, (
                        r.get("NHTSACampaignNumber") or r.get("nhtsaId") or f"{make_name}-{model_name}-{model_year}",
                        manufacturer_id, model_id, variant_id,
                        r.get("Component"), r.get("Summary"),
                        r.get("Consequence"), r.get("Remedy"),
                        _to_iso_date(r.get("ReportReceivedDate")),
                        "US",
                        str(r.get("PotentialNumberofUnitsAffected") or "").replace(",", ""),
                        json.dumps(r, ensure_ascii=False, default=str),
                        model_year,
                    ))
                    cur.execute("RELEASE SAVEPOINT sp_recall")
                    stats.inserted += 1
                except Exception as e:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT sp_recall")
                    stats.errors.append(f"recalls {f}: {e}")
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    log.info("[load:recalls] inserted=%d errors=%d", stats.inserted, len(stats.errors))
    return stats


def load_complaints(*, dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    root = _auto_raw_root() / "nhtsa_complaints"
    if not root.exists():
        log.warning("[load] complaints root missing: %s", root)
        return stats

    conn = get_connection()
    with conn.cursor() as cur:
        for f in root.glob("*/*/*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                stats.errors.append(f"complaints bad json {f}: {e}")
                continue
            for r in data.get("results") or []:
                cur.execute("SAVEPOINT sp_complaint")
                try:
                    # NHTSA complaints 의 make/model/year 은 top-level 이 아니라
                    # products[0].productMake / productModel / productYear 에 있음.
                    products = r.get("products") or []
                    p0 = products[0] if products else {}
                    make_name = p0.get("productMake") or r.get("make") or ""
                    model_name = p0.get("productModel") or r.get("model") or ""
                    py = p0.get("productYear") or r.get("modelYear")
                    try:
                        model_year = int(py) if py else None
                    except (TypeError, ValueError):
                        model_year = None

                    manufacturer_id, model_id, variant_id = _resolve_make_model_variant(
                        cur, make_name, model_name, model_year)

                    components = []
                    if r.get("components"):
                        components = [c.strip() for c in str(r["components"]).split(";") if c.strip()]

                    cur.execute("""
                        INSERT INTO auto.events_complaints
                          (source, source_complaint_no, manufacturer_id, model_id, variant_id,
                           components, summary, filed_date, incident_date, country,
                           raw, snapshot_year)
                        VALUES ('nhtsa', %s, %s, %s, %s, %s, %s,
                                NULLIF(%s, '')::date,
                                NULLIF(%s, '')::date,
                                'US', %s::jsonb, %s)
                        ON CONFLICT (source, source_complaint_no) DO UPDATE SET
                          manufacturer_id = COALESCE(EXCLUDED.manufacturer_id,
                                                     auto.events_complaints.manufacturer_id),
                          model_id        = COALESCE(EXCLUDED.model_id,
                                                     auto.events_complaints.model_id),
                          variant_id      = COALESCE(EXCLUDED.variant_id,
                                                     auto.events_complaints.variant_id),
                          raw             = EXCLUDED.raw,
                          ingested_at     = now()
                    """, (
                        r.get("odiNumber") or f"{make_name}-{model_name}-{model_year}-{r.get('id','')}",
                        manufacturer_id, model_id, variant_id,
                        components, r.get("summary"),
                        _to_iso_date(r.get("dateComplaintFiled")),
                        _to_iso_date(r.get("dateOfIncident")),
                        json.dumps(r, ensure_ascii=False, default=str),
                        model_year,
                    ))
                    cur.execute("RELEASE SAVEPOINT sp_complaint")
                    stats.inserted += 1
                except Exception as e:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT sp_complaint")
                    stats.errors.append(f"complaint {f}: {e}")
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    log.info("[load:complaints] inserted=%d errors=%d", stats.inserted, len(stats.errors))
    return stats


def load_wikidata(*, dry_run: bool = False) -> LoadStats:
    """wikidata manufacturer/supplier QID 매핑을 master 테이블 메타에 보강."""
    stats = LoadStats()
    root = _auto_raw_root() / "wikidata"
    if not root.exists():
        log.warning("[load] wikidata root missing: %s", root)
        return stats

    conn = get_connection()
    with conn.cursor() as cur:
        # manufacturers — 이름 매칭으로 QID 보강 (manufacturer_id 신규 발급 가능).
        for row in _iter_jsonl(root / "manufacturers.jsonl"):
            cur.execute("SAVEPOINT sp_wd_mfr")
            try:
                qid = row.get("mfr_qid")
                name = row.get("mfrLabel")
                if not (qid and name):
                    cur.execute("RELEASE SAVEPOINT sp_wd_mfr")
                    continue
                country = row.get("countryLabel")
                cur.execute("""
                    SELECT manufacturer_id FROM auto.master_manufacturers
                    WHERE name_norm = %s LIMIT 1
                """, (normalize_corp_name(name),))
                r = cur.fetchone()
                if r:
                    cur.execute("""
                        UPDATE auto.master_manufacturers
                           SET wikidata_qid = COALESCE(wikidata_qid, %s),
                               country = COALESCE(country, %s),
                               updated_at = now()
                         WHERE manufacturer_id = %s
                    """, (qid, country, r[0]))
                    stats.updated += 1
                else:
                    # Wikidata 단독 출처 — manufacturer_id 자체 seq 발급.
                    _ensure_manufacturer(cur,
                        name=name, source="wikidata", source_ref=qid,
                        country=country, wikidata_qid=qid)
                    stats.inserted += 1
                cur.execute("RELEASE SAVEPOINT sp_wd_mfr")
            except Exception as e:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_wd_mfr")
                stats.errors.append(f"wikidata mfr {row.get('mfr_qid')}: {e}")

        # models — manufacturer 가 매칭돼야 model 도 추가/보강.
        for row in _iter_jsonl(root / "models.jsonl"):
            cur.execute("SAVEPOINT sp_wd_model")
            try:
                qid = row.get("model_qid")
                name = row.get("modelLabel")
                mfr_name = row.get("mfrLabel")
                if not (qid and name and mfr_name):
                    cur.execute("RELEASE SAVEPOINT sp_wd_model")
                    continue
                cur.execute("""
                    SELECT manufacturer_id FROM auto.master_manufacturers
                    WHERE name_norm = %s LIMIT 1
                """, (normalize_corp_name(mfr_name),))
                mfr_row = cur.fetchone()
                if not mfr_row:
                    # 제조사가 아직 없는 경우 wikidata 출처로 생성
                    mfr_id = _ensure_manufacturer(cur,
                        name=mfr_name, source="wikidata", source_ref=row.get("mfr_qid"),
                        wikidata_qid=row.get("mfr_qid"))
                else:
                    mfr_id = mfr_row[0]
                _ensure_model(cur,
                    manufacturer_id=mfr_id, name=name, market="GLOBAL",
                    source="wikidata", source_ref=qid, wikidata_qid=qid)
                cur.execute("RELEASE SAVEPOINT sp_wd_model")
                stats.inserted += 1
            except Exception as e:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_wd_model")
                stats.errors.append(f"wikidata model {row.get('model_qid')}: {e}")

        # suppliers — auto.* 에는 supplier 테이블이 없으므로 bridge.corp_entity 에 candidate 로 적재.
        # 본 모듈은 master 적재만 담당 → suppliers 는 load_bridge 에서 처리.

    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    log.info("[load:wikidata] inserted=%d updated=%d errors=%d",
             stats.inserted, stats.updated, len(stats.errors))
    return stats


# ── 보조 ────────────────────────────────────────────────────
def _to_iso_date(s: str | None) -> str:
    """'MM/DD/YYYY' / 'DD/MM/YYYY' / 'YYYY-MM-DD' / None → 'YYYY-MM-DD' or ''.

    NHTSA 의 엔드포인트별 표기가 일관되지 않음:
      - /complaints/ : MM/DD/YYYY
      - /recalls/    : DD/MM/YYYY
    parts[0]>12 → DD/MM, parts[1]>12 → MM/DD 로 명확히 판정. 둘 다 ≤12 이면 MM/DD
    로 가정 (US 관습; 모호 케이스에선 무해).
    """
    if not s:
        return ""
    s = s.strip()
    if not s:
        return ""
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            a, b, yy = parts
            try:
                ai, bi = int(a), int(b)
            except ValueError:
                return s[:10]
            if ai > 12 and bi <= 12:        # DD/MM/YYYY
                dd, mm = ai, bi
            elif bi > 12 and ai <= 12:      # MM/DD/YYYY
                mm, dd = ai, bi
            else:                            # 둘 다 ≤12 (모호) — MM/DD 로 가정.
                mm, dd = ai, bi
            return f"{yy.zfill(4)}-{mm:02d}-{dd:02d}"
    return s[:10]


# ── CLI ─────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_pg")
    ap.add_argument("--source",
                    choices=["nhtsa_vpic", "nhtsa_recalls", "nhtsa_complaints",
                             "wikidata", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    fns = {
        "nhtsa_vpic":      load_vpic,
        "nhtsa_recalls":   load_recalls,
        "nhtsa_complaints": load_complaints,
        "wikidata":        load_wikidata,
    }
    if args.source == "all":
        # 순서 중요 — vpic → wikidata(QID 보강) → recalls → complaints
        for name in ["nhtsa_vpic", "wikidata", "nhtsa_recalls", "nhtsa_complaints"]:
            log.info("=== loading %s ===", name)
            fns[name](dry_run=args.dry_run)
    else:
        fns[args.source](dry_run=args.dry_run)


if __name__ == "__main__":
    main()
