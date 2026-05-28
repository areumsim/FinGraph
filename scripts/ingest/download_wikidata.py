#!/usr/bin/env python3
"""Wikidata SPARQL + Entity API → data/raw/wikidata/.

2단계:
  Step A — SPARQL 한 번에 한국 상장사 후보 ~수천 건 받아 raw/wikidata/candidates.json
           동시에 ticker 일치로 corp_code 매칭 → matched.jsonl
  Step B — matched 의 QID 별로 Entity API 호출 → raw/wikidata/entities/<qid>.json

Step B 는 297개 회사 × 1콜 → ~5분(rate-limit 1/s).

사용:
    python scripts/ingest/download_wikidata.py [--step a|b|both]
                                                 [--limit N] [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings
from autonexusgraph.db.postgres import get_pool
from autonexusgraph.ingestion._common import (
    CheckpointStore, RateLimiter, fetch_with_retry, get_rate_limiter, save_raw,
)
from autonexusgraph.ingestion.wikidata_client import WikidataClient


SELECT_TICKERS = """
SELECT em.id_value as ticker, em.corp_code, c.corp_name
  FROM master.entity_map em
  JOIN master.companies c ON c.corp_code = em.corp_code
 WHERE em.id_type = 'ticker'
   AND c.is_active = TRUE
"""


def step_a(force: bool = False) -> dict[str, str]:
    """SPARQL 한 번 → candidates.json + ticker 매칭."""
    candidates_path = get_settings().ingest_raw_dir / "wikidata" / "candidates.json"
    matched_path = get_settings().ingest_raw_dir / "wikidata" / "matched.jsonl"

    # PG 에서 ticker → corp_code map
    pool = get_pool()
    ticker_to_corp: dict[str, tuple[str, str]] = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(SELECT_TICKERS)
        for ticker, corp_code, corp_name in cur.fetchall():
            if ticker:
                ticker_to_corp[ticker.strip()] = (corp_code, corp_name)
    print(f"[step-a] PG ticker map: {len(ticker_to_corp)}")

    # SPARQL 호출 (없거나 force 면)
    if candidates_path.exists() and not force:
        print(f"[step-a] candidates.json 존재 — skip (force 로 재수집)")
        with candidates_path.open(encoding="utf-8") as f:
            raw_candidates = json.load(f)
    else:
        print("[step-a] SPARQL 쿼리 중 …")
        limiter = RateLimiter(per_sec=0.5)  # 첫 호출은 최대한 보수적
        limiter.acquire()
        with WikidataClient() as wd:
            cands = fetch_with_retry(wd.fetch_korean_listed_companies,
                                     on_retry=lambda i, e: print(f"  retry#{i} {e}"))
        raw_candidates = [c.__dict__ for c in cands]
        save_raw("wikidata", "candidates.json", raw_candidates)
        print(f"[step-a] candidates {len(raw_candidates)} 저장")

    # 매칭 — 다중 전략:
    # (1) ticker 정확 (가장 강함, 1.00)
    # (2) ISIN 'KR7<ticker>...' → ticker 추출 후 매칭 (1.00 — ISIN 은 글로벌 표준)
    # (3) label_ko 정규화 (0.85)
    # (4) label_en 정규화 (0.75)
    # 한 corp_code 에 더 강한 매칭이 있으면 약한 것 덮어쓰기 안 함.
    import re
    from autonexusgraph.ingestion._common import normalize_corp_name

    name_to_corp = {}
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT corp_code, corp_name FROM master.companies WHERE is_active=TRUE")
        for cc, nm in cur.fetchall():
            name_to_corp[normalize_corp_name(nm)] = (cc, nm)

    ISIN_KR_PATTERN = re.compile(r"^KR7(\d{6})")
    matched: dict[str, dict] = {}

    def _upsert(corp_code: str, corp_name: str, qid: str, by: str, conf: float, c: dict):
        prev = matched.get(corp_code)
        if prev and prev["confidence"] >= conf:
            return
        matched[corp_code] = {
            "corp_code": corp_code, "corp_name": corp_name,
            "qid": qid, "match_by": by, "confidence": conf,
            "candidate": c,
        }

    for c in raw_candidates:
        qid = c.get("qid")
        if not qid:
            continue
        # (1) ticker 정확
        t = c.get("ticker")
        if t and t.strip() in ticker_to_corp:
            cc, nm = ticker_to_corp[t.strip()]
            _upsert(cc, nm, qid, "ticker", 1.00, c)
            continue
        # (2) ISIN → ticker
        isin = c.get("isin")
        if isin:
            m = ISIN_KR_PATTERN.match(isin.strip())
            if m and m.group(1) in ticker_to_corp:
                cc, nm = ticker_to_corp[m.group(1)]
                _upsert(cc, nm, qid, "isin_ticker", 1.00, c)
                continue
        # (3) label_ko 정규화
        lab = c.get("label_ko")
        if lab:
            key = normalize_corp_name(lab)
            if key in name_to_corp:
                cc, nm = name_to_corp[key]
                _upsert(cc, nm, qid, "label_ko_normalized", 0.85, c)
                continue
        # (4) label_en 정규화
        lab_en = c.get("label_en")
        if lab_en:
            key = normalize_corp_name(lab_en)
            if key in name_to_corp:
                cc, nm = name_to_corp[key]
                _upsert(cc, nm, qid, "label_en_normalized", 0.75, c)

    # matched.jsonl 저장
    with matched_path.open("w", encoding="utf-8") as f:
        for m in matched.values():
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    print(f"[step-a] matched {len(matched)} / {len(ticker_to_corp)} "
          f"({100 * len(matched) / max(1, len(ticker_to_corp)):.1f}%)")
    from collections import Counter
    by_counts = Counter(m["match_by"] for m in matched.values())
    for k, v in by_counts.most_common():
        print(f"[step-a]   by {k:25s} {v:>4}")

    return {m["corp_code"]: m["qid"] for m in matched.values()}


def step_b(corp_to_qid: dict[str, str] | None = None, limit: int | None = None,
           force: bool = False) -> None:
    """matched corp 별 entity API 호출 → entities/<qid>.json."""
    matched_path = get_settings().ingest_raw_dir / "wikidata" / "matched.jsonl"
    if corp_to_qid is None:
        if not matched_path.exists():
            print("[step-b] matched.jsonl 없음 — step a 먼저", file=sys.stderr)
            sys.exit(2)
        corp_to_qid = {}
        with matched_path.open(encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                corp_to_qid[d["corp_code"]] = d["qid"]

    targets = list(corp_to_qid.items())
    if limit:
        targets = targets[:limit]
    print(f"[step-b] targets: {len(targets)}")

    ckpt = CheckpointStore("wikidata_entity")
    limiter = get_rate_limiter("wikidata")

    with WikidataClient() as wd:
        for i, (corp_code, qid) in enumerate(targets, 1):
            if ckpt.is_done(qid) and not force:
                continue
            limiter.acquire()
            try:
                entity = fetch_with_retry(
                    lambda: wd.fetch_entity(qid),
                    on_retry=lambda a, e: print(f"  retry#{a} {qid}: {e}"),
                )
                if entity is None:
                    ckpt.mark_failed(qid, "not_found")
                    continue
                save_raw("wikidata", f"entities/{qid}.json", entity)
                ckpt.mark_done(qid, {"corp_code": corp_code})
                if i % 20 == 0:
                    print(f"  [{i}/{len(targets)}] done={ckpt.stats.done} "
                          f"failed={ckpt.stats.failed}")
            except Exception as e:
                ckpt.mark_failed(qid, str(e))

    print(f"\n[step-b] done={ckpt.stats.done} failed={ckpt.stats.failed}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step", choices=["a", "b", "both"], default="both")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    corp_to_qid: dict[str, str] = {}
    if args.step in ("a", "both"):
        corp_to_qid = step_a(force=args.force)
    if args.step in ("b", "both"):
        step_b(corp_to_qid or None, limit=args.limit, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
