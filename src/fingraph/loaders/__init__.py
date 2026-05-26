"""JSONL → PostgreSQL 적재 로더.

사용:
    from fingraph.loaders import load_companies, load_filings, load_financials

각 로더는:
- batch INSERT ... ON CONFLICT DO UPDATE (idempotent)
- dry_run=True 시 SQL 만 생성하고 실행 X
- 통계 dict 반환 (inserted/updated/skipped/failed)
"""

from .chunks import embed_chunks, load_chunks
from .companies import load_companies
from .filings import load_filings
from .financials import load_financials
from .graph import load_graph_companies

__all__ = [
    "load_companies",
    "load_filings",
    "load_financials",
    "load_chunks",
    "embed_chunks",
    "load_graph_companies",
]
