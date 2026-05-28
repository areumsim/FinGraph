"""DART 정형 지배구조 → Neo4j 적재 (SUBSIDIARY_OF / EXECUTIVE_OF / MAJOR_SHAREHOLDER_OF).

입력: data/raw/dart_bulk/corp/<corp_code>/{subsidiaries,executives,shareholders}/{year}.jsonl
출력: Neo4j 관계

설계:
- 각 연도별 snapshot — relation 속성에 snapshot_date 보관
- 정합성: ownership_pct, role 등은 회사·연도 별로 다를 수 있음
- 멀티-edge 허용 (같은 (a, b) 가 연도 다르면 별도 edge)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

from ..config import get_settings
from ._common import LoadStats


# ── 자회사 / 관계회사 ─────────────────────────────────────────────
# DART API 응답 키 (otrCprInvstmntSttus):
#   inv_prm                 : 출자대상 회사명
#   bsis_blce_co            : 기말 잔액 (보유 주식 수)
#   bsis_blce_qota_rt       : 기말 지분율(%)
#   acqs_dispsl_inv_qy      : ...
# 매핑: bsis_blce_qota_rt ≥ 50 → SUBSIDIARY_OF, 10 ≤ pct < 50 → RELATED_TO

CYPHER_SUBSIDIARIES = """
UNWIND $rows AS r
MERGE (parent:Company {corp_code: r.parent_corp_code})
MERGE (child:Company {name: r.child_name})
ON CREATE SET child.created_at = datetime(),
              child.source     = 'dart_subsidiary_external'
WITH parent, child, r
FOREACH (_ IN CASE WHEN r.ownership_pct >= 50 THEN [1] ELSE [] END |
  MERGE (child)-[rel:SUBSIDIARY_OF {snapshot_date: date(r.snapshot_date)}]->(parent)
  SET rel.ownership_pct = r.ownership_pct,
      rel.rcept_year    = r.rcept_year,
      rel.source        = 'dart_otr_cpr_invstmnt',
      rel.extracted_at  = datetime()
)
FOREACH (_ IN CASE WHEN r.ownership_pct < 50 AND r.ownership_pct >= 5 THEN [1] ELSE [] END |
  MERGE (child)-[rel:RELATED_TO {snapshot_date: date(r.snapshot_date)}]->(parent)
  SET rel.ownership_pct = r.ownership_pct,
      rel.source        = 'dart_otr_cpr_invstmnt',
      rel.extracted_at  = datetime()
)
"""

# ── 임원진 ────────────────────────────────────────────────────────
# DART API 응답 키 (exctvSttus):
#   nm                     : 성명
#   ofcps                  : 직위 (사장/부회장/이사/감사 등)
#   rgist_exctv_at         : 등기임원 구분 (사내이사/사외이사/감사위원/기타)
#   fte_at                 : 상근 여부 (상근/비상근)
#   chrg_job               : 담당 업무
#   birth_ym               : 출생연월
#   tenure_end_on          : 임기만료일

CYPHER_EXECUTIVES = """
UNWIND $rows AS r
MERGE (c:Company {corp_code: r.corp_code})
// Person 자연키 = (name, birth_year). birth_year 미상은 -1 로 정규화 → 동명이인 안전 분리.
MERGE (p:Person {name: r.name, birth_year: coalesce(r.birth_year, -1)})
ON CREATE SET p.created_at = datetime(),
              p.source     = 'dart_executive'
SET p.gender = coalesce(r.gender, p.gender)
WITH p, c, r
MERGE (p)-[rel:EXECUTIVE_OF {role: r.role, snapshot_year: r.year}]->(c)
SET rel.registered    = r.registered,
    rel.full_time     = r.full_time,
    rel.duty          = r.duty,
    rel.tenure_end    = r.tenure_end,
    rel.source        = 'dart_exctv_sttus',
    rel.extracted_at  = datetime()
"""

# ── 최대주주 ───────────────────────────────────────────────────────
# DART API 응답 키 (hyslrSttus):
#   nm                          : 성명/법인명
#   relate                       : 관계 (최대주주 본인/특수관계인/임원 등)
#   stock_knd                    : 주식 종류
#   bsis_posesn_stock_co         : 기초 보유 주식 수
#   bsis_posesn_stock_qota_rt    : 기초 지분율
#   trmend_posesn_stock_co       : 기말 보유 주식 수
#   trmend_posesn_stock_qota_rt  : 기말 지분율

CYPHER_SHAREHOLDERS = """
UNWIND $rows AS r
MERGE (c:Company {corp_code: r.corp_code})
// 법인 주주 vs 개인 주주 구분 — 끝에 ㈜/주식회사/법인 이면 법인, 아니면 자연인
WITH c, r,
     CASE WHEN r.name =~ '.*(㈜|주식회사|\\\\(주\\\\)|Corp|Inc|Ltd|법인).*'
          THEN 'company' ELSE 'person' END AS holder_kind
FOREACH (_ IN CASE WHEN holder_kind = 'person' THEN [1] ELSE [] END |
  // 최대주주 보고서엔 birth_year 가 거의 없어 -1 로 정규화. 후속 ER 단계에서 보강.
  MERGE (h:Person {name: r.name, birth_year: -1})
  ON CREATE SET h.source = 'dart_shareholder'
  MERGE (h)-[rel:MAJOR_SHAREHOLDER_OF {snapshot_year: r.year, relation: r.relation}]->(c)
  SET rel.ownership_pct = r.ownership_pct,
      rel.stock_count   = r.stock_count,
      rel.source        = 'dart_hyslr_sttus',
      rel.extracted_at  = datetime()
)
FOREACH (_ IN CASE WHEN holder_kind = 'company' THEN [1] ELSE [] END |
  MERGE (h:Company {name: r.name})
  ON CREATE SET h.source = 'dart_shareholder_external'
  MERGE (h)-[rel:MAJOR_SHAREHOLDER_OF {snapshot_year: r.year, relation: r.relation}]->(c)
  SET rel.ownership_pct = r.ownership_pct,
      rel.stock_count   = r.stock_count,
      rel.source        = 'dart_hyslr_sttus',
      rel.extracted_at  = datetime()
)
"""


def _parse_pct(s) -> float | None:
    if s in (None, "", "-"):
        return None
    try:
        return float(str(s).replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return None


def _parse_int(s) -> int | None:
    if s in (None, "", "-"):
        return None
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_birth_year(birth_ym) -> int | None:
    """'196809' → 1968. None → None."""
    if not birth_ym:
        return None
    m = re.match(r"^(\d{4})", str(birth_ym))
    return int(m.group(1)) if m else None


def _iter_jsonl(p: Path) -> Iterator[dict]:
    if not p.exists() or p.stat().st_size == 0:
        return
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# ── 로더 본체 ──────────────────────────────────────────────────────

def load_subsidiaries(
    *, bulk_root: Path | None = None, dry_run: bool = False, batch_size: int = 500,
) -> LoadStats:
    s = get_settings()
    bulk_root = bulk_root or (s.ingest_raw_dir / "dart_bulk" / "corp")
    stats = LoadStats()

    rows: list[dict] = []
    for corp_dir in sorted(bulk_root.iterdir()):
        if not corp_dir.is_dir():
            continue
        sub_dir = corp_dir / "subsidiaries"
        if not sub_dir.exists():
            continue
        for fp in sorted(sub_dir.glob("*.jsonl")):
            year = int(fp.stem)
            for r in _iter_jsonl(fp):
                pct = _parse_pct(r.get("bsis_blce_qota_rt"))
                child_name = (r.get("inv_prm") or r.get("invstmnt_cmpny_nm") or "").strip()
                if not child_name or pct is None:
                    continue
                rows.append({
                    "parent_corp_code": corp_dir.name,
                    "child_name": child_name[:200],
                    "ownership_pct": pct,
                    "snapshot_date": f"{year}-12-31",
                    "rcept_year": year,
                })

    if dry_run:
        stats.inserted = len(rows)
        stats.batches = (len(rows) + batch_size - 1) // batch_size
        return stats

    from ..db.neo4j import get_driver
    with get_driver().session() as session:
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            session.run(CYPHER_SUBSIDIARIES, rows=batch)
            stats.inserted += len(batch)
            stats.batches += 1
    return stats


_ROLE_MAP = {
    "사내이사": "Inside Director",
    "사외이사": "Outside Director",
    "감사위원": "Audit Committee",
    "감사위원회 위원": "Audit Committee",
    "감사": "Auditor",
    "기타비상무이사": "Non-Executive Director",
    "기타": "Other",
}


def load_executives(
    *, bulk_root: Path | None = None, dry_run: bool = False, batch_size: int = 500,
) -> LoadStats:
    s = get_settings()
    bulk_root = bulk_root or (s.ingest_raw_dir / "dart_bulk" / "corp")
    stats = LoadStats()

    rows: list[dict] = []
    for corp_dir in sorted(bulk_root.iterdir()):
        if not corp_dir.is_dir():
            continue
        ex_dir = corp_dir / "executives"
        if not ex_dir.exists():
            continue
        for fp in sorted(ex_dir.glob("*.jsonl")):
            year = int(fp.stem)
            for r in _iter_jsonl(fp):
                name = (r.get("nm") or "").strip()
                if not name:
                    continue
                position = (r.get("ofcps") or "").strip()
                registered = (r.get("rgist_exctv_at") or "").strip()    # 사내/사외/감사위원/기타
                full_time = (r.get("fte_at") or "").strip()
                rows.append({
                    "corp_code": corp_dir.name,
                    "name": name[:100],
                    "birth_year": _parse_birth_year(r.get("birth_ym")),
                    "gender": (r.get("sexdstn") or None),
                    "role": registered or position or "기타",
                    "year": year,
                    "registered": registered,
                    "full_time": full_time,
                    "duty": (r.get("chrg_job") or "")[:200] or None,
                    "tenure_end": r.get("tenure_end_on") or None,
                })

    if dry_run:
        stats.inserted = len(rows)
        stats.batches = (len(rows) + batch_size - 1) // batch_size
        return stats

    from ..db.neo4j import get_driver
    with get_driver().session() as session:
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            session.run(CYPHER_EXECUTIVES, rows=batch)
            stats.inserted += len(batch)
            stats.batches += 1
    return stats


def load_shareholders(
    *, bulk_root: Path | None = None, dry_run: bool = False, batch_size: int = 500,
) -> LoadStats:
    s = get_settings()
    bulk_root = bulk_root or (s.ingest_raw_dir / "dart_bulk" / "corp")
    stats = LoadStats()

    rows: list[dict] = []
    for corp_dir in sorted(bulk_root.iterdir()):
        if not corp_dir.is_dir():
            continue
        sh_dir = corp_dir / "shareholders"
        if not sh_dir.exists():
            continue
        for fp in sorted(sh_dir.glob("*.jsonl")):
            year = int(fp.stem)
            for r in _iter_jsonl(fp):
                name = (r.get("nm") or "").strip()
                pct = _parse_pct(r.get("trmend_posesn_stock_qota_rt"))
                if not name or pct is None:
                    continue
                rows.append({
                    "corp_code": corp_dir.name,
                    "name": name[:200],
                    "year": year,
                    "relation": (r.get("relate") or "")[:50],
                    "ownership_pct": pct,
                    "stock_count": _parse_int(r.get("trmend_posesn_stock_co")),
                })

    if dry_run:
        stats.inserted = len(rows)
        stats.batches = (len(rows) + batch_size - 1) // batch_size
        return stats

    from ..db.neo4j import get_driver
    with get_driver().session() as session:
        for i in range(0, len(rows), batch_size):
            batch = rows[i:i + batch_size]
            session.run(CYPHER_SHAREHOLDERS, rows=batch)
            stats.inserted += len(batch)
            stats.batches += 1
    return stats


def load_all_structural() -> dict[str, LoadStats]:
    return {
        "subsidiaries":  load_subsidiaries(),
        "executives":    load_executives(),
        "shareholders":  load_shareholders(),
    }
