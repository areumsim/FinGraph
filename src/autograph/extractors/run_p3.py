"""P3 LLM 추출 — 자동차 도메인.

흐름:
  1) chunk_selector.select_auto_chunks(...) — vec.chunks 필터.
  2) (옵션) --dry-run-cost 시 estimate 만 출력하고 종료.
  3) ExtractorEngine([AutoRelationExtractor()]) 로 chunk 별 LLM 호출.
  4) merged relations → staging_writer.upsert_staging — auto.staging_relations 적재.
  5) (별도) cross_validate.run_p4 로 Neo4j 적재.

CLI:
    python -m autograph.extractors.run_p3 \\
        --manufacturer-ids 6486,6487 \\
        --sources nhtsa_recall,nhtsa_complaint \\
        --limit 200

    # 비용만 추정
    python -m autograph.extractors.run_p3 --manufacturer-ids 6486 --dry-run-cost
"""

from __future__ import annotations

import argparse
import logging
from typing import Sequence

from autonexusgraph.extractors.base import RunContext
from autonexusgraph.extractors.engine import ExtractorEngine
from autonexusgraph.llm.base import get_llm_client
from autonexusgraph.llm.budget_aware import budget_aware_client
from autonexusgraph.llm.cost import estimate

from .auto_relation_extractor import AutoRelationExtractor
from .chunk_selector import (
    DEFAULT_SOURCES,
    resolve_manufacturer_name,
    select_auto_chunks,
)
from .staging_writer import upsert_staging


log = logging.getLogger(__name__)


def estimate_cost(chunks: list[dict], model: str = "gpt-4o-mini"):
    """프롬프트 길이 평균 + chunk_text 합산 추정. dry-run-cost 출력용."""
    n = len(chunks)
    if n == 0:
        return None
    avg_chunk_chars = sum(len(c["text"]) for c in chunks) / n
    # system ~1500 (스키마 포함) + user_template ~600 + chunk_text/3 (한국어 보수)
    avg_in_tokens = 1500 + 600 + avg_chunk_chars / 3
    avg_out_tokens = 350
    return estimate(model, n, avg_in_tokens, avg_out_tokens)


def run(
    *,
    manufacturer_ids: Sequence[int] | None,
    model_ids: Sequence[int] | None,
    sources: Sequence[str],
    snapshot_years: Sequence[int] | None,
    limit: int,
    dry_run_cost: bool,
    hard_limit_usd: float | None,
) -> dict:
    chunks = select_auto_chunks(
        manufacturer_ids=manufacturer_ids,
        model_ids=model_ids,
        sources=sources or DEFAULT_SOURCES,
        snapshot_years=snapshot_years,
        limit=limit,
    )
    if not chunks:
        log.info("[run_p3] 0 chunks selected — skip")
        return {"chunks": 0}

    if dry_run_cost:
        est = estimate_cost(chunks)
        log.info("[run_p3] DRY-RUN-COST: %s", est)
        return {"chunks": len(chunks), "estimate": est}

    extractor = AutoRelationExtractor()
    inner = get_llm_client(role="research")
    client = budget_aware_client(inner, caller="auto_p3",
                                  hard_limit=hard_limit_usd)

    # manufacturer name resolver (LLM prompt hint).
    mfr_names: dict[int, str] = {}
    for c in chunks:
        mid = c.get("manufacturer_id")
        if mid and mid not in mfr_names:
            mfr_names[mid] = resolve_manufacturer_name(mid) or ""

    ctx = RunContext(
        llm_client=client,
        prompt_spec=extractor.prompt,
        extra={"manufacturer_names": mfr_names},
    )

    engine = ExtractorEngine([extractor], max_concurrency=1)
    all_rels: list[dict] = []
    for c in chunks:
        merged, _ = engine.process(c, ctx)
        all_rels.extend(merged)

    counts = upsert_staging(all_rels,
                            extractor_name=extractor.name,
                            extractor_version=extractor.version)

    log.info("[run_p3] chunks=%d relations=%d gate=%s",
             len(chunks), len(all_rels), counts)
    return {
        "chunks": len(chunks),
        "relations": len(all_rels),
        "gate": counts,
        "engine_stats": {
            "n_chunks": engine.stats.n_chunks,
            "n_extractor_calls": engine.stats.n_extractor_calls,
            "n_warnings": engine.stats.n_warnings,
            "total_latency_ms": engine.stats.total_latency_ms,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.extractors.run_p3")
    ap.add_argument("--manufacturer-ids", type=lambda s: [int(x) for x in s.split(",") if x],
                    default=None,
                    help="콤마 구분 (예: 6486,6487). 빈값=전체.")
    ap.add_argument("--model-ids", type=lambda s: [int(x) for x in s.split(",") if x],
                    default=None)
    ap.add_argument("--sources",
                    type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                    default=list(DEFAULT_SOURCES))
    ap.add_argument("--snapshot-years",
                    type=lambda s: [int(x) for x in s.split(",") if x.strip()],
                    default=None)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--dry-run-cost", action="store_true",
                    help="LLM 호출 없이 비용 추정만 출력")
    ap.add_argument("--hard-limit-usd", type=float, default=None,
                    help="BudgetTracker 의 hard limit (USD). 초과 시 BudgetExceeded")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    run(
        manufacturer_ids=args.manufacturer_ids,
        model_ids=args.model_ids,
        sources=args.sources,
        snapshot_years=args.snapshot_years,
        limit=args.limit,
        dry_run_cost=args.dry_run_cost,
        hard_limit_usd=args.hard_limit_usd,
    )


if __name__ == "__main__":
    main()


__all__ = ["run", "estimate_cost"]
