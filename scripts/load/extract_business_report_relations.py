#!/usr/bin/env python3
"""P3 — 사업보고서 본문 청크 → LLM 관계 추출 진입점.

처리 흐름:
1. PG vec.chunks 에서 대상 청크 SELECT (회사 / 연도 / section / 토큰수 필터)
2. 청크 묶음에 대해 dry-run 비용 추정 → BudgetCheck 통과
3. LLMClient (budget_aware wrapper) 호출 — 누적 비용 한도 도달 시 즉시 중단
4. 결과는 data/processed/extracted/<corp_code>/<rcept_no>.jsonl 에 append
5. P4 검증 + Neo4j 적재는 별도 스크립트 (load_validated_relations.py)

사용자 명시 비용 가드 (memory: feedback-llm-cost-brake) — 모든 진입점에 강제:
- --dry-run            : LLM 호출 0, 추정만 출력
- --approve-cost       : auto_approve 초과 추정 자동 통과
- --max-cost USD       : 이 호출 한정 hard_limit override
- --limit N            : 처음 N 청크만

예시:
  python scripts/load/extract_business_report_relations.py \\
      --top-by-market-cap 30 --year 2024 --dry-run

  python scripts/load/extract_business_report_relations.py \\
      --top-by-market-cap 30 --year 2024 --max-cost 1.0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _select_top_corps(n: int) -> list[str]:
    """시가총액 상위 N 회사의 corp_code. PoC 범위 제한용."""
    from autonexusgraph.db.postgres import get_pool
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT corp_code
              FROM master.companies
             WHERE is_active = TRUE
               AND stock_code IS NOT NULL
             ORDER BY (extra->>'market_cap_krw')::numeric DESC NULLS LAST
             LIMIT %s
        """, (n,))
        return [r[0] for r in cur.fetchall()]


def _load_company_names(corps: list[str]) -> dict[str, str]:
    """corp_code → corp_name 룩업 (프롬프트 컨텍스트용)."""
    from autonexusgraph.db.postgres import get_pool
    out: dict[str, str] = {}
    if not corps:
        return out
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT corp_code, corp_name FROM master.companies WHERE corp_code = ANY(%s)", (corps,))
        for cc, nm in cur.fetchall():
            out[cc] = nm
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="P3 사업보고서 → LLM 관계 추출")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--top-by-market-cap", type=int, default=30,
                        help="시가총액 상위 N 회사만 (기본 30, PoC 규모)")
    target.add_argument("--corp-codes", type=str, default=None,
                        help="쉼표 구분 corp_code 목록 (PoC 외 분석용)")

    parser.add_argument("--year", type=int, action="append", default=None,
                        help="회계연도 필터 (반복 가능). 미지정 시 전 연도.")
    parser.add_argument("--limit-chunks-per-corp-year", type=int, default=5,
                        help="회사·연도·섹션 당 최대 청크 (호출 폭증 방지)")
    parser.add_argument("--limit", type=int, default=None,
                        help="처음 N 청크만 (전체 cap)")

    # 비용 가드 표준 옵션 — 모든 LLM 진입점 공통.
    parser.add_argument("--dry-run", action="store_true",
                        help="추정만 출력, LLM 호출 0")
    parser.add_argument("--approve-cost", action="store_true",
                        help="추정이 auto_approve 초과해도 자동 진행")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="이 호출 한정 hard_limit (USD). LLM_COST_HARD_LIMIT_USD 무시")

    parser.add_argument("--model-role", default="research",
                        help="LLM 역할 키 (.env 의 LLM_MODEL_<ROLE>). 기본 research")

    parser.add_argument("--force", action="store_true",
                        help="이미 처리된 청크도 재호출")

    args = parser.parse_args()

    # 대상 corp 결정
    if args.corp_codes:
        corps = [c.strip() for c in args.corp_codes.split(",") if c.strip()]
    else:
        corps = _select_top_corps(args.top_by_market_cap)

    print(f"[P3] target corps: {len(corps)} (sample: {corps[:5]})")

    from autonexusgraph.extractors.llm_relations import (
        filter_target_chunks, estimate_p3_cost, load_prompt, extract_one, save_result,
    )
    from autonexusgraph.config import get_settings
    settings = get_settings()

    # 1) 대상 청크 SELECT
    chunks = filter_target_chunks(
        corp_codes=corps,
        fiscal_years=args.year,
        limit_per_corp_year=args.limit_chunks_per_corp_year,
    )
    if args.limit:
        chunks = chunks[:args.limit]

    print(f"[P3] candidate chunks: {len(chunks)} (after section/token/limit filter)")

    # 멱등: 이미 처리된 청크 skip
    processed_root = settings.ingest_processed_dir / "extracted"
    if not args.force:
        processed = _existing_chunk_ids(processed_root)
        before = len(chunks)
        chunks = [c for c in chunks if c["id"] not in processed]
        print(f"[P3] skip already processed: {before - len(chunks)} (force=False)")

    if not chunks:
        print("[P3] 처리할 청크 없음. 종료.")
        return 0

    # 2) 비용 추정 + gate
    from autonexusgraph.llm.cost import BudgetCheck

    model_name = _resolve_model(settings, args.model_role)
    est = estimate_p3_cost(chunks, model=model_name)
    print(est.format())

    gate = BudgetCheck.from_env(caller="p3_extract")
    gate.review(est, approve_cost=args.approve_cost,
                max_cost_override=args.max_cost, dry_run=args.dry_run)
    # gate.review 가 dry_run 이면 SystemExit(0). 통과하면 계속.

    # 3) LLM client + tracker
    from autonexusgraph.llm.base import get_llm_client
    from autonexusgraph.llm.budget_aware import budget_aware_client
    from autonexusgraph.llm.cost_tracker import BudgetExceeded, reset_tracker

    reset_tracker()
    inner = get_llm_client(role=args.model_role)
    hard = args.max_cost if args.max_cost is not None else None
    client = budget_aware_client(inner, caller="p3_extract", hard_limit=hard)

    # 회사명 prefetch
    company_names = _load_company_names(corps)
    prompt = load_prompt()

    # 4) 청크별 처리
    ok = 0
    failed = 0
    try:
        for i, chunk in enumerate(chunks, 1):
            res = extract_one(
                chunk,
                company_name_resolver=company_names,
                client=client,
                prompt=prompt,
                model_role=args.model_role,
                purpose="p3_extract",
            )
            if res is None:
                failed += 1
                continue
            save_result(res, root=processed_root)
            ok += 1
            if i % 10 == 0:
                print(f"  [{i}/{len(chunks)}] ok={ok} failed={failed}")
    except BudgetExceeded as e:
        print(f"\n[P3] BUDGET EXCEEDED — abort. {e}")
        client._tracker.finalize("aborted_budget")
        return 4

    client._tracker.finalize("ok")
    print(f"\n[P3] done ok={ok} failed={failed} chunks={len(chunks)}")
    return 0


def _existing_chunk_ids(root: Path) -> set[int]:
    """processed/extracted/ 에 이미 적재된 청크 id 집합 (force=False 일 때 skip)."""
    ids: set[int] = set()
    if not root.exists():
        return ids
    for jl in root.rglob("*.jsonl"):
        try:
            for line in jl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                if "chunk_id" in d:
                    ids.add(int(d["chunk_id"]))
        except Exception:
            continue
    return ids


def _resolve_model(settings, role: str) -> str:
    """역할별 모델 결정 (.env 의 LLM_MODEL_<ROLE>)."""
    attr = f"llm_model_{role}"
    return getattr(settings, attr, None) or settings.llm_model


if __name__ == "__main__":
    raise SystemExit(main())
