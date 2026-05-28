#!/usr/bin/env python3
"""DART 임원 + 최대주주(자연인) → master.persons + person_executive_history.

선행: data/raw/dart_bulk/corp/<corp_code>/executives/<year>.jsonl 존재
목적: PG 에 인물 SSOT 확보 — Neo4j 와 비교/검증·시계열 JOIN 분석 가능.

동명이인 처리:
- (canonical_name, birth_year) 가 UNIQUE → 같은 이름이라도 birth_year 다르면 별도 인물
- birth_year NULL 인 경우는 같은 이름끼리 1명으로 묶음 (notes 에 'birth_year unknown')

사용:
    python scripts/load/load_persons.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_pool


UPSERT_PERSON = """
INSERT INTO master.persons (canonical_name, birth_year, aliases)
VALUES (%s, %s, %s)
ON CONFLICT (canonical_name, birth_year) DO UPDATE
   SET aliases    = master.persons.aliases || EXCLUDED.aliases,
       updated_at = now()
RETURNING internal_id
"""

UPSERT_HISTORY = """
INSERT INTO master.person_executive_history
  (internal_id, corp_code, role, registered, since_date, until_date, rcept_no, raw)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (internal_id, corp_code, role, since_date, rcept_no) DO UPDATE
   SET registered = EXCLUDED.registered,
       until_date = EXCLUDED.until_date,
       raw        = EXCLUDED.raw
"""


def _iter_jsonl(p: Path):
    if not p.exists() or p.stat().st_size == 0:
        return
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_birth_year(birth_ym) -> int | None:
    if not birth_ym:
        return None
    m = re.match(r"^(\d{4})", str(birth_ym))
    return int(m.group(1)) if m else None


def _parse_date(s) -> str | None:
    """YYYYMMDD/YYYY-MM-DD → ISO date string. 실패 시 None."""
    if not s:
        return None
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="최대 처리할 corp 수")
    args = parser.parse_args()

    s = get_settings()
    bulk = s.ingest_raw_dir / "dart_bulk" / "corp"

    persons: dict[tuple[str, int | None], dict] = {}
    history_rows: list[dict] = []

    corp_dirs = sorted([d for d in bulk.iterdir() if d.is_dir()])
    if args.limit:
        corp_dirs = corp_dirs[: args.limit]

    for corp_dir in corp_dirs:
        ex_dir = corp_dir / "executives"
        if not ex_dir.exists():
            continue
        for fp in sorted(ex_dir.glob("*.jsonl")):
            year = int(fp.stem)
            for r in _iter_jsonl(fp):
                name = (r.get("nm") or "").strip()
                if not name:
                    continue
                birth = _parse_birth_year(r.get("birth_ym"))
                key = (name, birth)
                if key not in persons:
                    persons[key] = {
                        "canonical_name": name[:100],
                        "birth_year": birth,
                        "aliases": [],
                    }
                position = (r.get("ofcps") or "").strip()
                registered_raw = (r.get("rgist_exctv_at") or "").strip()
                role = registered_raw or position or "기타"
                history_rows.append({
                    "key": key,
                    "corp_code": corp_dir.name,
                    "role": role[:50],
                    "registered": ("등기" in registered_raw) if registered_raw else None,
                    "since_date": None,        # DART API 응답에 시점 없음 — 보고서 연도로 대체
                    "until_date": _parse_date(r.get("tenure_end_on")),
                    "rcept_no": f"snap_{year}",  # 스냅샷 가상 키 — 실제 rcept_no 매핑은 후속 작업
                    "raw": r,
                })

    print(f"[persons] unique={len(persons):,}  history_rows={len(history_rows):,}")
    if args.dry_run:
        for k, v in list(persons.items())[:5]:
            print("  P:", k, v)
        return 0

    pool = get_pool()
    name_to_id: dict[tuple[str, int | None], str] = {}

    # 1) persons upsert
    with pool.connection() as conn, conn.cursor() as cur:
        for key, p in persons.items():
            cur.execute(UPSERT_PERSON, (p["canonical_name"], p["birth_year"], p["aliases"]))
            internal_id = cur.fetchone()[0]
            name_to_id[key] = internal_id

    # 2) history insert (배치)
    with pool.connection() as conn, conn.cursor() as cur:
        params = []
        for h in history_rows:
            pid = name_to_id.get(h["key"])
            if not pid:
                continue
            params.append((
                pid, h["corp_code"], h["role"], h["registered"],
                h["since_date"], h["until_date"], h["rcept_no"],
                json.dumps(h["raw"], ensure_ascii=False),
            ))
        # executemany batch
        BATCH = 1000
        for i in range(0, len(params), BATCH):
            cur.executemany(UPSERT_HISTORY, params[i:i + BATCH])

    # 검증
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM master.persons")
        n_p = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM master.person_executive_history")
        n_h = cur.fetchone()[0]
        cur.execute("""
            SELECT role, count(*) FROM master.person_executive_history
            GROUP BY role ORDER BY 2 DESC LIMIT 8
        """)
        roles = cur.fetchall()

    print(f"\n[persons] master.persons rows: {n_p:,}")
    print(f"[persons] person_executive_history rows: {n_h:,}")
    print(f"[persons] top roles:")
    for r, c in roles:
        print(f"  {r:30s} {c:>7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
