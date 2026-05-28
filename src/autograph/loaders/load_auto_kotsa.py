"""한국교통안전공단 자동차검사관리_수리검사내역 → vec.chunks 통계 청크.

data.go.kr 15155857. CSV 2개 (EUC-KR):
- 수리검사내역(UVTOTLOSSRS_T).csv — 49,290 row, [검사소코드, 접수일자, 접수일련번호, 접수횟수, 특이사항코드]
- 검사소코드설명.csv — 검사소코드 → 검사소명

수리검사 = 사고/침수/도난/기타 사유로 운행정지된 차량의 안전 재검사. **VIN/차량모델
정보가 없어** 차량 단위 매칭 불가. 따라서 본 loader 는 통계적 청크로만 변환:

- 연도 × 특이사항 분포 (예: 2020 사고 12,345건 / 침수 234건 / 도난 3건)
- 검사소 상위 N개 × 특이사항 분포 (지역별 분포 파악)

vec.chunks 에 manufacturer_id/model_id 없이 (NULL) 적재되므로 `search_documents_auto`
는 의미 검색 (한국어 RAG) 에만 사용 — 차량 메타 필터 무관.

CLI:
    python -m autograph.loaders.load_auto_kotsa
    python -m autograph.loaders.load_auto_kotsa --dry-run
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from autonexusgraph.db.postgres import get_connection
from ..config import get_auto_settings


log = logging.getLogger(__name__)


@dataclass
class LoadStats:
    rows_seen: int = 0
    chunks_inserted: int = 0
    chunks_updated: int = 0
    errors: list[str] = field(default_factory=list)


def _read_inspections(csv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with csv_path.open(encoding="euc-kr") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def _read_station_codes(csv_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not csv_path.exists():
        return out
    with csv_path.open(encoding="euc-kr") as f:
        for r in csv.DictReader(f):
            code = (r.get("코드") or "").strip()
            name = (r.get("코드설명") or "").strip()
            if code:
                out[code] = name
    return out


def aggregate(rows: list[dict]) -> dict:
    by_year_cat: dict[tuple[str, str], int] = collections.Counter()
    by_station: dict[str, int] = collections.Counter()
    by_cat: dict[str, int] = collections.Counter()
    for r in rows:
        date = (r.get("접수일자") or "").strip()
        year = date[:4] if len(date) >= 4 else "?"
        cat = (r.get("특이사항코드") or "").strip().strip('"')
        station = (r.get("검사소코드") or "").strip()
        by_year_cat[(year, cat)] += 1
        by_cat[cat] += 1
        if station:
            by_station[station] += 1
    return {
        "by_year_cat": dict(by_year_cat),
        "by_station": dict(by_station),
        "by_cat": dict(by_cat),
        "total": len(rows),
    }


def _format_chunk_overall(agg: dict, station_names: dict) -> str:
    lines = [
        "한국교통안전공단 자동차검사관리 수리검사내역 (data.go.kr 15155857)",
        f"총 수리검사 건수: {agg['total']:,}건",
        "",
        "특이사항코드별 분포:",
    ]
    for cat, n in sorted(agg["by_cat"].items(), key=lambda kv: -kv[1]):
        if cat:
            lines.append(f"  - {cat}: {n:,}건 ({100*n/agg['total']:.1f}%)")
    lines.append("")
    lines.append("연도별·사유별 발생:")
    rows_by_year: dict[str, dict[str, int]] = {}
    for (year, cat), n in agg["by_year_cat"].items():
        rows_by_year.setdefault(year, {})[cat] = n
    for year in sorted(rows_by_year):
        parts = [f"{cat}={n:,}" for cat, n in sorted(rows_by_year[year].items(), key=lambda kv: -kv[1])]
        lines.append(f"  {year}: {' / '.join(parts)}")
    lines.append("")
    lines.append("상위 10 검사소 (수리검사 처리 건수):")
    top = sorted(agg["by_station"].items(), key=lambda kv: -kv[1])[:10]
    for code, n in top:
        name = station_names.get(code, "(이름 미상)")
        lines.append(f"  - {code} {name}: {n:,}건")
    lines.append("")
    lines.append("수리검사란 사고·침수·도난 등 사유로 운행정지된 차량이 안전 재검사를 받은 기록.")
    lines.append("VIN/차량모델 정보 없음 (차량 식별 불가능). 통계 분석 목적의 집계 데이터.")
    lines.append("출처: 한국교통안전공단(TS), data.go.kr 15155857.")
    return "\n".join(lines)


def _upsert_chunk(cur, *, uniq: str, source: str, text: str, metadata: dict) -> str:
    cur.execute("""
        SELECT id, text FROM vec.chunks
         WHERE source = %s AND metadata->>'uniq' = %s
         LIMIT 1
    """, (source, uniq))
    r = cur.fetchone()
    if r:
        cid, ex_text = r
        if ex_text != text:
            cur.execute("""
                UPDATE vec.chunks SET text=%s, token_count=%s,
                       metadata = metadata || %s::jsonb,
                       embedding = NULL
                 WHERE id = %s
            """, (text, max(1, len(text) // 4),
                  json.dumps(metadata, ensure_ascii=False, default=str), cid))
            return "updated"
        return "skipped"
    cur.execute("""
        INSERT INTO vec.chunks
          (corp_code, rcept_no, section, chunk_idx, text, token_count,
           metadata, source, manufacturer_id, model_id, variant_id)
        VALUES (NULL, NULL, %s, 0, %s, %s, %s::jsonb, %s, NULL, NULL, NULL)
    """, (metadata.get("section", "auto.inspection"), text, max(1, len(text) // 4),
          json.dumps(metadata, ensure_ascii=False, default=str), source))
    return "inserted"


def load(*, dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    s = get_auto_settings()
    root = s.datagokr_kotsa_inspection_dir
    if not root.exists():
        log.warning("[load:kotsa] root missing: %s", root)
        return stats

    csv_files = list(root.glob("수리검사내역*.csv"))
    station_files = list(root.glob("검사소코드*.csv"))
    if not csv_files:
        log.warning("[load:kotsa] CSV 없음 — data/raw/datagokr/ 에 다운로드 ZIP 압축 해제 필요")
        return stats

    station_names = _read_station_codes(station_files[0]) if station_files else {}
    rows: list[dict] = []
    for f in csv_files:
        rows.extend(_read_inspections(f))
    stats.rows_seen = len(rows)
    log.info("[load:kotsa] read %d rows from %d csv(s)", stats.rows_seen, len(csv_files))
    if not rows:
        return stats

    agg = aggregate(rows)
    text = _format_chunk_overall(agg, station_names)
    uniq = "datagokr_15155857::overall"
    # JSON metadata 의 dict key 는 string 만 허용 — (year, cat) tuple → "year|cat" 직렬화.
    year_categories = {f"{y}|{c}": n for (y, c), n in agg["by_year_cat"].items()}
    metadata = {
        "uniq": uniq, "section": "auto.inspection.summary",
        "dataset_id": 15155857,
        "total_inspections": agg["total"],
        "year_categories": year_categories,
        "categories": agg["by_cat"],
    }

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp_kotsa")
        try:
            op = _upsert_chunk(cur, uniq=uniq, source="datagokr_kotsa_inspection",
                               text=text, metadata=metadata)
            cur.execute("RELEASE SAVEPOINT sp_kotsa")
            if op == "inserted":
                stats.chunks_inserted += 1
            elif op == "updated":
                stats.chunks_updated += 1
        except Exception as e:  # noqa: BLE001
            cur.execute("ROLLBACK TO SAVEPOINT sp_kotsa")
            stats.errors.append(f"kotsa chunk: {e}")

    if dry_run:
        conn.rollback()
        log.info("[load:kotsa] DRY-RUN — text preview:\n%s", text[:400])
    else:
        conn.commit()
        log.info("[load:kotsa] chunks inserted=%d updated=%d errors=%d",
                 stats.chunks_inserted, stats.chunks_updated, len(stats.errors))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_kotsa")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
