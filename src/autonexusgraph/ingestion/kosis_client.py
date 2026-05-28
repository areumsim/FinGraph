"""KOSIS 통계청 OpenAPI 클라이언트 (kosis.kr/openapi).

라이선스: 공공저작물 (출처 표기).
키 발급: https://kosis.kr/openapi/ — 무료, 일 호출량 충분.

본 시스템 활용 통계 (예시):
- 광업·제조업 동향조사 (DT_1F31013S)        — 산업별 생산/출하/재고
- 산업분류별 매출액 (DT_1G80001)            — 산업 단위 매출
- 경제활동인구조사 (DT_1DA7001)             — 거시지표
- 한국표준산업분류 (KSIC)                   — Industry 노드 분류 매핑
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


KOSIS_BASE = "https://kosis.kr/openapi/Param/statisticsParameterData.do"


@dataclass(frozen=True)
class KosisRow:
    stat_code: str
    item_code: str
    time: str
    value: float | None
    unit: str | None
    stat_name: str | None
    item_name: str | None


class KosisClient:
    """KOSIS statisticsParameterData API."""

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        if not api_key:
            raise ValueError(
                "KOSIS_API_KEY 미설정 — kosis.kr/openapi 에서 무료 키 발급 후 .env 추가"
            )
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def __enter__(self) -> "KosisClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self._client.close()

    def fetch_series(
        self,
        org_id: str,        # 통계작성기관 (e.g., '101' 통계청, '301' 한국은행)
        tbl_id: str,        # 통계표 ID
        period_type: str,   # 'A' 연간, 'M' 월간, 'Q' 분기, 'D' 일간
        start: str,         # 'YYYY' or 'YYYYMM' 등
        end: str,
        item_codes: list[str] | None = None,
        obj_l1: list[str] | None = None,
    ) -> list[dict]:
        """statisticsParameterData — 통계 시계열 raw."""
        params: dict[str, Any] = {
            "method":      "getList",
            "apiKey":      self.api_key,
            "orgId":       org_id,
            "tblId":       tbl_id,
            "prdSe":       period_type,
            "startPrdDe":  start,
            "endPrdDe":    end,
            "format":      "json",
            "jsonVD":      "Y",
        }
        if item_codes:
            params["itmId"] = "+".join(item_codes)
        if obj_l1:
            params["objL1"] = "+".join(obj_l1)
        resp = self._client.get(KOSIS_BASE, params=params)
        resp.raise_for_status()
        return resp.json()

    def normalize(self, raw_rows: list[dict], stat_code_hint: str = "") -> list[KosisRow]:
        """KOSIS 응답 → 표준 KosisRow.

        KOSIS 응답 키 패턴:
          TBL_ID, TBL_NM, ITM_ID, ITM_NM, PRD_DE, PRD_SE, DT, UNIT_NM, ...
        """
        out: list[KosisRow] = []
        for r in raw_rows:
            try:
                val_s = r.get("DT")
                val = float(val_s) if val_s not in ("", None, "-") else None
            except (ValueError, TypeError):
                val = None
            out.append(KosisRow(
                stat_code=r.get("TBL_ID") or stat_code_hint,
                item_code=r.get("ITM_ID", ""),
                time=str(r.get("PRD_DE", "")),
                value=val,
                unit=r.get("UNIT_NM"),
                stat_name=r.get("TBL_NM"),
                item_name=r.get("ITM_NM"),
            ))
        return out
