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
    (auto.spec_measurements 은 본 모듈이 만들지 않는다 — Canadian specs 의 dim/weight
     키 매핑은 `load_auto_specs.py` 가 담당. 본 모듈은 마스터·이벤트만.)

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

import psycopg

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name


# SAVEPOINT 패턴 안에서 흡수해도 안전한 예외 종류 — DB 적재 시 row-level 데이터
# 결함 (FK, type, 누락, ON CONFLICT 외 unique 위배) 만. AttributeError 같은
# 코드 버그는 의도적으로 raise (fail-fast).
_ROW_LEVEL_ERRORS = (psycopg.Error, ValueError, TypeError, KeyError)


log = logging.getLogger(__name__)


# ── 모델명 noise 필터 ────────────────────────────────────────
# NHTSA vPIC 의 GetModelsForMakeYear 는 같은 makes 이름을 공유하는 자회사·관계사
# (예 "Hyundai Steel Industries, Inc.", "Hyundai Translead Trailers", Genesis RV
# 트레일러의 "Tahoe"/"Supreme"/"Envy" 같은) 도 모두 반환. 자동차 모델이 아닌 row 를
# master_vehicle_models 에 적재하면 vehicle 검색·매칭이 오염된다.
#
# 보수적 deny rule (의심스러우면 keep):
#   1. 회사 접미사 패턴 (Inc / Corp / Industries / Ltd / Co\. / LLC / GmbH / N\.V\.)
#   2. 알려진 자회사·부문 키워드 (Mobis / Steel / Translead / Trailers)
import re as _re
# False positive 회피: "Ford LTD" (실제 옛 차종), "Honda Civic" 같은 일반 모델은 keep.
# 명확한 회사 접미사 — comma + Inc/Corp/Industries 또는 끝에 'Inc.'/'Corp.'/'LLC' 등.
_NOISE_MODEL_PATTERNS = (
    # ', Inc.' / ' Inc.' 끝 — 회사 접미사. comma 유무 무관.
    _re.compile(r'(?:,\s*|\s+)(Inc|Corp|Industries|LLC|GmbH|S\.A\.|N\.V\.)\.?\s*$', _re.I),
    # 자회사 키워드.
    _re.compile(r'\b(Mobis|Translead)\b', _re.I),
    # 끝이 'Trailers' / 'Trailer' — Genesis/Hyundai 트레일러 자회사.
    _re.compile(r'\bTrailers?\s*$', _re.I),
    # 'Steel Industries' — Hyundai Steel 등 제철 자회사.
    _re.compile(r'\bSteel\s+Industries\b', _re.I),
)


def _is_noise_model_name(name: str | None) -> bool:
    """차종 모델 이름이 아니라고 의심되는 row 판별. True 면 reject.

    보수적 — 명확한 자회사·트레일러 신호만. 'Ford LTD' 같은 실차종은 keep.
    """
    if not name or not name.strip():
        return True
    s = name.strip()
    for pat in _NOISE_MODEL_PATTERNS:
        if pat.search(s):
            return True
    return False


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
    """make+model+year → (manufacturer_id, model_id, variant_id).

    vPIC 의 brand 중복 (예: FORD 가 mfr_id 460/1237/5697/8578/... 10+) 케이스에서
    model_name 매칭 가능한 mfr 을 우선 선택. 이전 LEFT JOIN + LIMIT 1 은 첫 mfr
    만 선택해 model_id NULL 적재되는 버그가 있었음.

    우선순위:
        1. (make, model) 직접 INNER JOIN — model_name 매칭하는 mfr 선택
           (variant year 도 매칭하면 그 row 선택 — 더 구체적)
        2. model_name 매칭 없으면 brand 만 — manufacturer_id 만 반환

    매칭 실패한 슬롯은 NULL.
    """
    if not make_name:
        return None, None, None

    make_norm = normalize_corp_name(make_name)
    model_norm = normalize_corp_name(model_name) if model_name else None

    # 1) (make, model) 매칭 — model_year 일치 variant 우선.
    if model_norm:
        cur.execute("""
            SELECT mm.manufacturer_id, m.model_id, v.variant_id
              FROM auto.master_vehicle_models m
              JOIN auto.master_manufacturers mm USING (manufacturer_id)
              LEFT JOIN auto.master_vehicle_variants v
                ON v.model_id = m.model_id
               AND v.model_year = %s::int
             WHERE mm.name_norm = %s AND m.name_norm = %s
             ORDER BY (v.variant_id IS NULL) ASC, m.model_id ASC
             LIMIT 1
        """, (model_year, make_norm, model_norm))
        row = cur.fetchone()
        if row:
            return row[0], row[1], row[2]

    # 2) brand 만 매칭 — 어떤 mfr 이든 첫 매칭 (model_id, variant_id NULL).
    cur.execute("""
        SELECT manufacturer_id FROM auto.master_manufacturers
         WHERE name_norm = %s
         ORDER BY manufacturer_id ASC LIMIT 1
    """, (make_norm,))
    row = cur.fetchone()
    if not row:
        return None, None, None
    return row[0], None, None


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
                    drive_type: str | None = None,
                    transmission: str | None = None,
                    source: str, source_ref: str | None,
                    raw: dict | None = None) -> int:
    """variants UPSERT — 부분 unique index 사용으로 COALESCE 정합.

    SELECT 후 존재하면 빈 값(body_class/drive_type/transmission)을 보강하기 위해 UPDATE 도 시도.
    이렇게 해야 vPIC 1차 적재(이름만) 이후 canspec 2차 적재(body/drive 보강)가 같은 row 를 채운다.
    """
    cur.execute("""
        SELECT variant_id, body_class, drive_type, transmission, fuel_type
          FROM auto.master_vehicle_variants
        WHERE model_id = %s AND model_year = %s
          AND COALESCE(trim, '') = COALESCE(%s, '')
          AND COALESCE(fuel_type, '') = COALESCE(%s, '')
        LIMIT 1
    """, (model_id, model_year, trim, fuel_type))
    r = cur.fetchone()
    if r:
        vid = r[0]
        # NULL 컬럼만 보강 (이미 값이 있으면 유지).
        cur.execute("""
            UPDATE auto.master_vehicle_variants
               SET body_class  = COALESCE(body_class,  %s),
                   drive_type  = COALESCE(drive_type,  %s),
                   transmission= COALESCE(transmission,%s),
                   fuel_type   = COALESCE(fuel_type,   %s)
             WHERE variant_id = %s
               AND (body_class IS NULL OR drive_type IS NULL
                    OR transmission IS NULL OR fuel_type IS NULL)
        """, (body_class, drive_type, transmission, fuel_type, vid))
        return vid
    cur.execute("""
        INSERT INTO auto.master_vehicle_variants
          (model_id, model_year, trim, fuel_type, body_class,
           drive_type, transmission, source, source_ref, raw)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING variant_id
    """, (model_id, model_year, trim, fuel_type, body_class,
          drive_type, transmission,
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
                except _ROW_LEVEL_ERRORS as e:
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

                    # noise 모델 (회사 접미사·자회사·트레일러 등) skip.
                    if _is_noise_model_name(model_name):
                        cur.execute("RELEASE SAVEPOINT sp_vpic_var")
                        stats.skipped += 1
                        continue

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
                    # GetModelsForMakeYear 는 trim/fuel/body 를 반환하지 않으므로 이 시점에선
                    # (model_id, year) 한 행만 생성. canspec / wikipedia / DecodeVin 같은
                    # 후속 source 가 _ensure_variant 호출로 body_class·drive_type 등을 보강한다.
                    _ensure_variant(cur,
                        model_id=model_id,
                        model_year=model_year,
                        source="nhtsa_vpic",
                        source_ref=f"{make_id}/{row.get('model_id_vpic')}/{model_year}",
                        raw=row.get("raw") or {})
                    cur.execute("RELEASE SAVEPOINT sp_vpic_var")
                    stats.inserted += 1
                except _ROW_LEVEL_ERRORS as e:
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
    _COMMIT_EVERY = 500     # SAVEPOINT/subtxn 누적 방지를 위한 mid-commit interval.
    _rows_since_commit = 0
    with conn.cursor() as cur:
        for f in root.glob("*/*/*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                stats.errors.append(f"recalls bad json {f}: {e}")
                continue

            for r in data.get("results") or []:
                # row 별 savepoint — 한 row 가 실패해도 트랜잭션 전체 abort 되지 않게.
                # SAVEPOINT 누적 회피: _COMMIT_EVERY 도달마다 mid-commit → subtxn ID reset.
                cur.execute("SAVEPOINT sp_recall")
                try:
                    make_name = r.get("Make") or ""
                    model_name = r.get("Model") or ""
                    model_year = int(r.get("ModelYear") or 0) or None

                    manufacturer_id, model_id, variant_id = _resolve_make_model_variant(
                        cur, make_name, model_name, model_year)

                    # §6.7 의 snapshot_year 는 "관측 시점" — report_date 의 연도.
                    # report_date 가 없으면 적재 연도 fallback. 차량 model_year 와는 별개.
                    report_date_iso = _to_iso_date(r.get("ReportReceivedDate"))
                    cur.execute("""
                        INSERT INTO auto.events_recalls
                          (source, source_recall_no, manufacturer_id, model_id, variant_id,
                           component_text, defect_summary, consequence, remedy_summary,
                           report_date, country, affected_units, raw, snapshot_year)
                        VALUES ('nhtsa', %s, %s, %s, %s, %s, %s, %s, %s,
                                NULLIF(%s, '')::date, %s,
                                NULLIF(%s, '')::bigint,
                                %s::jsonb,
                                COALESCE(
                                  EXTRACT(YEAR FROM NULLIF(%s, '')::date)::SMALLINT,
                                  EXTRACT(YEAR FROM now())::SMALLINT))
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
                        report_date_iso,
                        "US",
                        str(r.get("PotentialNumberofUnitsAffected") or "").replace(",", ""),
                        json.dumps(r, ensure_ascii=False, default=str),
                        report_date_iso,
                    ))
                    cur.execute("RELEASE SAVEPOINT sp_recall")
                    stats.inserted += 1
                except _ROW_LEVEL_ERRORS as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_recall")
                    stats.errors.append(f"recalls {f}: {e}")

                _rows_since_commit += 1
                if not dry_run and _rows_since_commit >= _COMMIT_EVERY:
                    conn.commit()
                    _rows_since_commit = 0
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
    _COMMIT_EVERY = 500     # 16k complaints — SAVEPOINT 누적 차단 mid-commit.
    _rows_since_commit = 0
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

                    # snapshot_year = filed_date 연도. 없으면 incident_date 연도. 그것도 없으면 적재 연도.
                    filed_iso = _to_iso_date(r.get("dateComplaintFiled"))
                    incident_iso = _to_iso_date(r.get("dateOfIncident"))
                    cur.execute("""
                        INSERT INTO auto.events_complaints
                          (source, source_complaint_no, manufacturer_id, model_id, variant_id,
                           components, summary, filed_date, incident_date, country,
                           raw, snapshot_year)
                        VALUES ('nhtsa', %s, %s, %s, %s, %s, %s,
                                NULLIF(%s, '')::date,
                                NULLIF(%s, '')::date,
                                'US', %s::jsonb,
                                COALESCE(
                                  EXTRACT(YEAR FROM NULLIF(%s, '')::date)::SMALLINT,
                                  EXTRACT(YEAR FROM NULLIF(%s, '')::date)::SMALLINT,
                                  EXTRACT(YEAR FROM now())::SMALLINT))
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
                        filed_iso,
                        incident_iso,
                        json.dumps(r, ensure_ascii=False, default=str),
                        filed_iso,
                        incident_iso,
                    ))
                    cur.execute("RELEASE SAVEPOINT sp_complaint")
                    stats.inserted += 1
                except _ROW_LEVEL_ERRORS as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_complaint")
                    stats.errors.append(f"complaint {f}: {e}")

                _rows_since_commit += 1
                if not dry_run and _rows_since_commit >= _COMMIT_EVERY:
                    conn.commit()
                    _rows_since_commit = 0
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
            except _ROW_LEVEL_ERRORS as e:
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
            except _ROW_LEVEL_ERRORS as e:
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
_AMBIGUOUS_DATE_WARNED: set[str] = set()   # 동일 파일에서 반복 경고 억제.


def _to_iso_date(s: str | None) -> str:
    """'MM/DD/YYYY' / 'DD/MM/YYYY' / 'YYYY-MM-DD' / None → 'YYYY-MM-DD' or ''.

    NHTSA 의 엔드포인트별 표기가 일관되지 않음:
      - /complaints/ : MM/DD/YYYY
      - /recalls/    : DD/MM/YYYY
    parts[0]>12 → DD/MM, parts[1]>12 → MM/DD 로 명확히 판정.
    둘 다 ≤12 이면 모호 — MM/DD 로 가정 (US 관습) + log.warning (sample 만).
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
            else:                            # 둘 다 ≤12 (모호) — MM/DD 가정.
                mm, dd = ai, bi
                if s not in _AMBIGUOUS_DATE_WARNED and len(_AMBIGUOUS_DATE_WARNED) < 10:
                    _AMBIGUOUS_DATE_WARNED.add(s)
                    log.warning(
                        "[date] 모호 케이스 %r — MM/DD 가정 → %s-%02d-%02d "
                        "(첫 10개만 경고)", s, yy.zfill(4), mm, dd,
                    )
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
