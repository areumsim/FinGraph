"""data/raw/auto/nhtsa_mfrcomm/FLAT_TSBS.zip → vec.chunks (source='nhtsa_tsb').

NHTSA Manufacturer Communications (TSB / Campaign / Warranty / OTA / Emissions / Other)
의 SUMMARY 본문을 narrative 청크로 변환. Investigations/Recalls/Complaints 와는 다른
"OEM 측 자발적 통신문" — 결함 / 알려진 문제 / 수리 지침 등 richer text.

PRD §3.5 등급: **A** (0.90) — OEM 공식 제출, NHTSA 정제. 본 PR 은 source='nhtsa_tsb'
한 종류로 통합 적재 (Communication Type 은 metadata 에 보존).

TAB-delimited 14 컬럼 — schema 는 ingestion 모듈의 docstring 참조.

variant 매칭:
    (Make, Model, ModelYear) → master_vehicle_variants 모두 매칭 (NCAP loader 패턴).
    매칭된 variant_id 가 0개면 model_id 만 채움. 그것도 0개면 manufacturer_id 만.

청크 구조:
    text = "유형: {COMM_TYPE}\\n부품: {NHTSA_COMPONENTS}\\n시스템: {MFR_SYSTEM} / {SUBSYS}\\n요약: {SUMMARY}"
    metadata = {NHTSA_ID, TSB_DOC_ID, MFR_INTERNAL_CAMPAIGN_ID, MFR_COMM_DATE,
                COMM_TYPE, MAKE, MODEL, MODEL_YEAR, NHTSA_COMPONENTS, ...}
    section = "auto.mfrcomm"
    source = "nhtsa_tsb"

CLI:
    python -m autograph.loaders.load_auto_mfrcomm
    python -m autograph.loaders.load_auto_mfrcomm --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name


log = logging.getLogger(__name__)


_SOURCE_KEY = "nhtsa_tsb"
_SECTION = "auto.mfrcomm"
_CONFIDENCE = 0.90

# FLAT_TSBS.zip 안 TAB-delimited 14 컬럼 (no header).
_COLUMNS = (
    "NHTSA_ID_NUMBER",
    "REPLACEMENT_SERVICE_BULLETIN_NO",
    "DATE_ADDED_TO_FILE",
    "TSB_DOCUMENT_ID",
    "MFR_COMMUNICATION_DATE",
    "MFR_INTERNAL_CAMPAIGN_ID",
    "COMMUNICATION_TYPE",
    "MAKE",
    "MODEL",
    "MODEL_YEAR",
    "NHTSA_COMPONENTS",
    "MFR_COMPONENT_SYSTEM",
    "MFR_COMPONENT_SUBSYSTEM",
    "SUMMARY",
)


@dataclass
class LoadStats:
    rows_seen:        int = 0
    rows_unmatched:   int = 0
    rows_inserted:    int = 0
    rows_updated:     int = 0
    rows_skipped:     int = 0
    variants_touched: int = 0
    errors: list[str] = field(default_factory=list)


def _mfrcomm_root() -> Path:
    return get_settings().ingest_raw_dir / "auto" / "nhtsa_mfrcomm"


def _find_zip() -> Path | None:
    """FLAT_TSBS.zip 또는 FLAT_MFRCOMM.zip 또는 *.zip 첫 매치."""
    root = _mfrcomm_root()
    for name in ("FLAT_TSBS.zip", "FLAT_MFRCOMM.zip"):
        p = root / name
        if p.exists():
            return p
    # 다른 이름 zip 도 시도.
    cands = sorted(root.glob("*.zip"))
    return cands[0] if cands else None


def _iter_rows(zip_path: Path) -> Iterator[dict[str, str]]:
    """zip 안 TAB-delimited txt → dict (no header)."""
    with zipfile.ZipFile(zip_path) as z:
        txt_names = [n for n in z.namelist()
                     if n.upper().endswith((".TXT", ".CSV"))]
        if not txt_names:
            raise FileNotFoundError(f"zip 안 txt/csv 없음: {zip_path}")
        with z.open(txt_names[0]) as f:
            wrapper = io.TextIOWrapper(
                f, encoding="utf-8-sig", errors="replace", newline="",
            )
            reader = csv.reader(wrapper, delimiter="\t", quoting=csv.QUOTE_NONE)
            for row in reader:
                if not row:
                    continue
                values = list(row) + [""] * (len(_COLUMNS) - len(row))
                yield dict(zip(_COLUMNS, values[: len(_COLUMNS)]))


def _parse_year(s: str | None) -> int | None:
    if not s:
        return None
    s = str(s).strip()
    if not s.isdigit():
        return None
    y = int(s)
    if y == 9999 or y < 1900 or y > 2099:
        return None
    return y


def _resolve_targets(cur, *, make: str, model: str, year: int | None
                     ) -> tuple[int | None, int | None, list[int]]:
    """(make, model, year) → (manufacturer_id, model_id, [variant_ids]).

    variants 는 동일 (model, year) variant 모두 (NCAP 패턴). year 없으면 빈 리스트.
    """
    if not make:
        return None, None, []
    cur.execute("""
        SELECT mm.manufacturer_id, m.model_id
          FROM auto.master_manufacturers mm
          LEFT JOIN auto.master_vehicle_models m
            ON m.manufacturer_id = mm.manufacturer_id
           AND m.name_norm = %s
         WHERE mm.name_norm = %s
         LIMIT 1
    """, (
        normalize_corp_name(model) if model else None,
        normalize_corp_name(make),
    ))
    r = cur.fetchone()
    if not r:
        return None, None, []
    mfr_id, model_id = r[0], r[1]

    variants: list[int] = []
    if model_id and year:
        cur.execute("""
            SELECT variant_id
              FROM auto.master_vehicle_variants
             WHERE model_id = %s AND model_year = %s
        """, (model_id, year))
        variants = [row[0] for row in cur.fetchall()]
    return mfr_id, model_id, variants


def _compose_text(row: dict[str, str]) -> str:
    parts: list[str] = []
    ct = (row.get("COMMUNICATION_TYPE") or "").strip()
    if ct:
        parts.append(f"유형: {ct}")
    comps = (row.get("NHTSA_COMPONENTS") or "").strip()
    if comps:
        parts.append(f"부품 (NHTSA): {comps}")
    mfr_sys = (row.get("MFR_COMPONENT_SYSTEM") or "").strip()
    mfr_sub = (row.get("MFR_COMPONENT_SUBSYSTEM") or "").strip()
    if mfr_sys or mfr_sub:
        parts.append(f"시스템 (제조사): {mfr_sys}"
                     + (f" / {mfr_sub}" if mfr_sub else ""))
    summary = (row.get("SUMMARY") or "").strip()
    if summary:
        parts.append(f"요약: {summary}")
    return "\n".join(parts).strip()


def _upsert_chunk(cur, *, uniq: str, text: str, metadata: dict,
                  manufacturer_id: int | None,
                  model_id: int | None,
                  variant_id: int | None) -> str:
    """vec.chunks UPSERT — source='nhtsa_tsb' + metadata.uniq dedup.

    Returns: 'inserted' | 'updated' | 'skipped'.
    """
    cur.execute("""
        SELECT id, text FROM vec.chunks
         WHERE source = %s AND metadata->>'uniq' = %s
         LIMIT 1
    """, (_SOURCE_KEY, uniq))
    r = cur.fetchone()
    if r:
        cid, ex_text = r
        if ex_text == text:
            return "skipped"
        cur.execute("""
            UPDATE vec.chunks
               SET text = %s, token_count = %s,
                   manufacturer_id = COALESCE(manufacturer_id, %s),
                   model_id        = COALESCE(model_id, %s),
                   variant_id      = COALESCE(variant_id, %s),
                   embedding       = NULL,
                   metadata        = metadata || %s::jsonb
             WHERE id = %s
        """, (text, max(1, len(text) // 4),
              manufacturer_id, model_id, variant_id,
              json.dumps(metadata, ensure_ascii=False, default=str), cid))
        return "updated"
    cur.execute("""
        INSERT INTO vec.chunks
          (corp_code, rcept_no, section, chunk_idx, text, token_count,
           metadata, source, manufacturer_id, model_id, variant_id)
        VALUES (NULL, NULL, %s, 0, %s, %s, %s::jsonb, %s, %s, %s, %s)
    """, (_SECTION, text, max(1, len(text) // 4),
          json.dumps(metadata, ensure_ascii=False, default=str),
          _SOURCE_KEY, manufacturer_id, model_id, variant_id))
    return "inserted"


def _process_row(cur, row: dict[str, str], stats: LoadStats) -> None:
    stats.rows_seen += 1
    nhtsa_id = (row.get("NHTSA_ID_NUMBER") or "").strip()
    summary = (row.get("SUMMARY") or "").strip()
    if not (nhtsa_id and summary):
        stats.rows_skipped += 1
        return

    make = (row.get("MAKE") or "").strip()
    model = (row.get("MODEL") or "").strip()
    year = _parse_year(row.get("MODEL_YEAR"))

    mfr_id, model_id, variant_ids = _resolve_targets(
        cur, make=make, model=model, year=year,
    )
    if not (mfr_id or model_id or variant_ids):
        stats.rows_unmatched += 1
        # 그래도 청크는 생성 — narrative 검색은 가능.

    text = _compose_text(row)
    if not text:
        stats.rows_skipped += 1
        return

    # variant 단위 청크 — 매칭된 variant 별로 1청크 (동일 본문 dedup metadata.uniq 분리).
    # variant 0개면 model_id 단위로 1청크. 그것도 없으면 manufacturer_id 만.
    if variant_ids:
        targets = [(mfr_id, model_id, vid) for vid in variant_ids]
    else:
        targets = [(mfr_id, model_id, None)]

    metadata_base = {
        "nhtsa_id":     nhtsa_id,
        "tsb_doc_id":   row.get("TSB_DOCUMENT_ID") or None,
        "mfr_internal": row.get("MFR_INTERNAL_CAMPAIGN_ID") or None,
        "comm_type":    row.get("COMMUNICATION_TYPE") or None,
        "comm_date":    row.get("MFR_COMMUNICATION_DATE") or None,
        "added_date":   row.get("DATE_ADDED_TO_FILE") or None,
        "make":         make, "model": model, "year": year,
        "components":   row.get("NHTSA_COMPONENTS") or None,
        "mfr_system":   row.get("MFR_COMPONENT_SYSTEM") or None,
        "mfr_subsys":   row.get("MFR_COMPONENT_SUBSYSTEM") or None,
    }

    for mid, modid, vid in targets:
        uniq_key = f"nhtsa_tsb::{nhtsa_id}::{vid if vid is not None else 'model'}::{modid or 'mfr'}::{mid or 'na'}"
        metadata = {**metadata_base, "uniq": uniq_key}
        cur.execute("SAVEPOINT sp_tsb")
        try:
            op = _upsert_chunk(cur,
                uniq=uniq_key, text=text, metadata=metadata,
                manufacturer_id=mid, model_id=modid, variant_id=vid,
            )
            cur.execute("RELEASE SAVEPOINT sp_tsb")
            if op == "inserted":
                stats.rows_inserted += 1
                stats.variants_touched += 1 if vid else 0
            elif op == "updated":
                stats.rows_updated += 1
            else:
                stats.rows_skipped += 1
        except Exception as e:   # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT sp_tsb")
            stats.errors.append(f"{nhtsa_id}/{vid}: {e}")


def load_mfrcomm(*, dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    zip_path = _find_zip()
    if zip_path is None:
        log.warning(
            "[load:mfrcomm] zip 없음 — autograph.ingestion.nhtsa_mfrcomm 의 "
            "안내대로 수동 다운로드 후 %s 에 배치.",
            _mfrcomm_root(),
        )
        return stats

    conn = get_connection()
    with conn.cursor() as cur:
        for row in _iter_rows(zip_path):
            _process_row(cur, row, stats)

    if dry_run:
        conn.rollback()
        log.info(
            "[load:mfrcomm] DRY-RUN seen=%d ins=%d upd=%d skipped=%d "
            "unmatched=%d errors=%d",
            stats.rows_seen, stats.rows_inserted, stats.rows_updated,
            stats.rows_skipped, stats.rows_unmatched, len(stats.errors),
        )
        return stats

    conn.commit()
    log.info(
        "[load:mfrcomm] seen=%d ins=%d upd=%d skipped=%d unmatched=%d errors=%d",
        stats.rows_seen, stats.rows_inserted, stats.rows_updated,
        stats.rows_skipped, stats.rows_unmatched, len(stats.errors),
    )
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_mfrcomm")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_mfrcomm(dry_run=args.dry_run)


if __name__ == "__main__":
    main()


__all__ = [
    "load_mfrcomm", "LoadStats",
    "_iter_rows", "_parse_year", "_resolve_targets",
    "_compose_text", "_upsert_chunk", "_process_row",
    "_COLUMNS", "_SOURCE_KEY", "_SECTION", "_CONFIDENCE",
]
