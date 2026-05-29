"""GLEIF LEI 클라이언트 (api.gleif.org).

라이선스: CC BY 4.0
키 불필요. rate limit: 60 req/min.

API:
  https://api.gleif.org/api/v1/lei-records?filter[entity.jurisdiction]=KR&page[size]=200
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx


GLEIF_BASE = "https://api.gleif.org/api/v1"


@dataclass(frozen=True)
class LeiRecord:
    lei: str
    legal_name: str | None
    jurisdiction: str | None
    entity_status: str | None
    registration_status: str | None
    issued_at: str | None
    next_renewal_at: str | None
    raw: dict


class GleifClient:
    def __init__(self, timeout: float = 60.0) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": "AutoNexusGraph-Research/0.1 (ifkbn@kolon.com)",
                "Accept": "application/vnd.api+json",
            },
        )

    def __enter__(self) -> "GleifClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self._client.close()

    def iter_korea(self, page_size: int = 200) -> Iterator[LeiRecord]:
        """한국 jurisdiction 의 LEI 전부 (페이지네이션)."""
        page = 1
        while True:
            url = f"{GLEIF_BASE}/lei-records"
            params = {
                "filter[entity.jurisdiction]": "KR",
                "page[number]": page,
                "page[size]": page_size,
            }
            resp = self._client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            records = data.get("data", [])
            if not records:
                break
            for r in records:
                yield _parse_record(r)
            meta = data.get("meta", {}).get("pagination", {})
            last = meta.get("lastPage")
            if not last or page >= last:
                break
            page += 1


def _parse_record(rec: dict) -> LeiRecord:
    attrs = rec.get("attributes", {})
    entity = attrs.get("entity", {})
    reg = attrs.get("registration", {})
    return LeiRecord(
        lei=rec.get("id", ""),
        legal_name=(entity.get("legalName") or {}).get("name"),
        jurisdiction=entity.get("jurisdiction"),
        entity_status=entity.get("status"),
        registration_status=reg.get("status"),
        issued_at=reg.get("initialRegistrationDate"),
        next_renewal_at=reg.get("nextRenewalDate"),
        raw=rec,
    )
