#!/usr/bin/env python3
"""eval/qa_gold/*.jsonl 스키마 + DB 엔티티 lint.

검사:
1. 필수 필드 (qid / question / question_type / complexity / domain) 존재.
2. qid prefix 일치 (FIN-L?-*, AUTO-L?-*, CD-L?-*).
3. domain 값 (finance / auto / cross_domain).
4. evidence_corp_codes 는 master.companies 에 실재 (DB 가용 시).
5. requires_multi_hop=true 또는 complexity='hard' 인데 hop_count<2 면 경고.
6. is_answerable=true 인데 gold_answer_text 비어있으면 경고.

사용:
    python scripts/audit/validate_gold_qa.py eval/qa_gold/*.jsonl

종료 코드:
    0: 에러 없음
    1: 1개 이상 에러
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path


REQUIRED_FIELDS = ("qid", "question", "question_type", "complexity")

VALID_QUESTION_TYPE = {
    "single_entity", "multi_entity", "relation",
    "aggregation", "ranking", "comparison",
}
VALID_COMPLEXITY = {"easy", "medium", "hard"}
VALID_DOMAIN = {"finance", "auto", "cross_domain"}

QID_PREFIX_RE = re.compile(
    r"^(FIN|AUTO|CD|EX)-?L?(\d|CD-?L\d)?[-_]?\d{0,4}$",
    re.IGNORECASE,
)


def _load_jsonl(path: Path) -> list[tuple[int, dict]]:
    out: list[tuple[int, dict]] = []
    if not path.exists():
        return out
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        try:
            out.append((i, json.loads(s)))
        except json.JSONDecodeError as exc:
            out.append((i, {"_parse_error": str(exc)}))
    return out


def _check_row(path: Path, lineno: int, row: dict,
               corp_codes_in_db: set[str] | None) -> list[tuple[str, str]]:
    msgs: list[tuple[str, str]] = []   # (level, message)

    def err(msg: str) -> None:
        msgs.append(("ERROR", msg))

    def warn(msg: str) -> None:
        msgs.append(("WARN", msg))

    if "_parse_error" in row:
        err(f"jsonl parse 실패: {row['_parse_error']}")
        return msgs

    # 1. 필수 필드.
    for f in REQUIRED_FIELDS:
        if not row.get(f):
            err(f"{f} 누락")

    qt = row.get("question_type")
    if qt and qt not in VALID_QUESTION_TYPE:
        err(f"question_type 비표준: {qt!r} (허용: {sorted(VALID_QUESTION_TYPE)})")
    cx = row.get("complexity")
    if cx and cx not in VALID_COMPLEXITY:
        err(f"complexity 비표준: {cx!r}")

    dom = row.get("domain")
    fname = path.name
    expected_dom: str | None = None
    if "cross" in fname:
        expected_dom = "cross_domain"
    elif "auto" in fname:
        expected_dom = "auto"
    elif fname.startswith("gold_qa_v") or "fin" in fname or "example" in fname:
        expected_dom = "finance"
    if dom and dom not in VALID_DOMAIN:
        err(f"domain 비표준: {dom!r}")
    if expected_dom and dom and dom != expected_dom:
        warn(f"domain={dom!r} 이 파일명({fname})과 불일치 — 기대 {expected_dom!r}")

    # 2. qid prefix.
    qid = row.get("qid", "")
    if dom == "cross_domain" and not qid.upper().startswith(("CD-", "CD0", "AUTO0", "FIN0")):
        warn(f"cross_domain qid 권장 prefix CD-Ln-: {qid!r}")
    if dom == "auto" and not qid.upper().startswith(("AUTO", "CD-")):
        warn(f"auto qid 권장 prefix AUTO-: {qid!r}")

    # 3. multi_hop / complexity 정합.
    if row.get("complexity") == "hard":
        if not row.get("requires_multi_hop") and int(row.get("hop_count") or 0) < 2:
            warn(f"complexity=hard 인데 requires_multi_hop=false & hop_count<2")

    # 4. is_answerable & gold_answer_text.
    if row.get("is_answerable", True):
        if not row.get("gold_answer_text") and not row.get("gold_answer_entities"):
            warn("is_answerable=true 인데 gold_answer_text / entities 모두 비어있음")
    else:
        if row.get("gold_answer_text"):
            warn("is_answerable=false 인데 gold_answer_text 가 채워져 있음 (refusal 평가 일관성)")

    # 5. evidence_corp_codes 실재 확인 (DB 가용 시).
    if corp_codes_in_db is not None:
        for cc in row.get("evidence_corp_codes") or []:
            if cc and cc not in corp_codes_in_db:
                err(f"evidence_corp_code={cc!r} 가 master.companies 에 없음")

    return msgs


def _load_corp_codes_from_db() -> set[str] | None:
    """master.companies.corp_code 전체. DB 가용 시 set, 실패 시 None (lint 4번 skip)."""
    try:
        from autograph.tools._db import query_dicts
        rows = query_dicts("SELECT corp_code FROM master.companies", ())
        return {str(r["corp_code"]) for r in rows if r.get("corp_code")}
    except Exception:
        return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="+", help="qa_gold/*.jsonl glob 또는 파일들")
    p.add_argument("--no-db", action="store_true",
                   help="DB 조회 (evidence_corp_codes 실재 확인) 스킵")
    p.add_argument("--strict", action="store_true",
                   help="WARN 도 exit 1 로 처리")
    args = p.parse_args()

    paths: list[Path] = []
    for p_arg in args.paths:
        if "*" in p_arg or "?" in p_arg:
            paths.extend(Path(x) for x in glob.glob(p_arg))
        else:
            paths.append(Path(p_arg))
    paths = [p for p in paths if p.is_file()]
    if not paths:
        print("[lint] 검사할 파일 없음", file=sys.stderr)
        return 2

    cc_set = None if args.no_db else _load_corp_codes_from_db()
    if cc_set is None and not args.no_db:
        print("[lint] DB 미가용 — evidence_corp_codes 검사 스킵", file=sys.stderr)

    errs = 0
    warns = 0
    for path in paths:
        rows = _load_jsonl(path)
        if not rows:
            print(f"  {path}: (비어있음)")
            continue
        file_errs = 0
        file_warns = 0
        for lineno, row in rows:
            msgs = _check_row(path, lineno, row, cc_set)
            for level, m in msgs:
                if level == "ERROR":
                    file_errs += 1
                    print(f"  ✗ {path}:{lineno} qid={row.get('qid','?')} {m}")
                else:
                    file_warns += 1
                    print(f"  ! {path}:{lineno} qid={row.get('qid','?')} {m}")
        print(f"  → {path.name}: {len(rows)} rows, "
              f"{file_errs} errors, {file_warns} warnings")
        errs += file_errs
        warns += file_warns

    print(f"\n[lint] 합계: {errs} errors, {warns} warnings")
    if errs:
        return 1
    if args.strict and warns:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
