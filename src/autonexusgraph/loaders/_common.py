"""로더 공통 유틸 — JSONL iter, batch executor, dry-run printer."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LoadStats:
    """로더 실행 통계."""

    inserted: int = 0      # 새 row
    updated: int = 0       # UPSERT 갱신
    skipped: int = 0       # 조건상 적재 안 함 (e.g., 잘못된 row)
    failed: int = 0        # 실제 실패
    batches: int = 0
    sql_preview: list[str] = field(default_factory=list)   # dry-run 용

    def summary(self) -> str:
        return (f"inserted={self.inserted:,} updated={self.updated:,} "
                f"skipped={self.skipped:,} failed={self.failed:,} "
                f"batches={self.batches:,}")


def iter_jsonl(path: Path) -> Iterator[dict]:
    """JSONL 파일 → dict iterator. 빈 줄·잘못된 줄은 skip."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def chunked(it: Iterator[Any], size: int) -> Iterator[list[Any]]:
    """generator → batch list iterator."""
    batch: list[Any] = []
    for item in it:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def parse_amount(s: Any) -> int | None:
    """DART 응답의 금액 문자열을 int 로. '1,234,567' → 1234567. 빈값/'-' → None."""
    if s is None or s == "" or s == "-":
        return None
    try:
        return int(str(s).replace(",", "").replace("(", "-").replace(")", ""))
    except (ValueError, TypeError):
        return None


def parse_int(s: Any) -> int | None:
    """단순 int 변환 — 실패 시 None."""
    if s is None or s == "":
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def parse_date(s: Any) -> str | None:
    """YYYYMMDD → 'YYYY-MM-DD' (PG DATE). 그대로 들어가면 그대로 반환."""
    if not s:
        return None
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s
