"""KRX 상장 종목 마스터 클라이언트.

KRX 공식 MDC 엔드포인트는 OTP + 세션 + 변경 잦음 → fragile.
대신 잘 유지보수되는 `FinanceDataReader` (KRX/네이버 기반) 사용.

PRD §3.2:
- 상장사 마스터 (종목코드/회사명/시장/업종/시가총액)
- KOSPI/KOSDAQ 시가총액 상위 N → KOSPI200/KOSDAQ100 대용
  (공식 지수와 약간 차이 있지만 "주요 종목" 목적엔 충분)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Listing:
    """상장 종목 1건."""

    stock_code: str
    name: str
    market: str           # KOSPI / KOSDAQ / KONEX
    market_cap: int | None  # KRW
    sector: str | None
    isin: str | None


class KrxClient:
    """FDR 기반 KRX 마스터 + 시가총액 조회.

    의존성: `pip install finance-datareader` (pyproject [ingest] extra).
    """

    def __init__(self, **_kwargs: Any) -> None:
        # 의존성 지연 import — 패키지 미설치 시 autonexusgraph import 는 가능하게
        import FinanceDataReader as fdr  # noqa: N813
        self._fdr = fdr

    def __enter__(self) -> "KrxClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    # ---- 마스터 ----

    def fetch_listed_companies(self, market: str = "KOSPI"):
        """전체 상장 종목 마스터 DataFrame 반환.

        market: KOSPI | KOSDAQ | KONEX | KRX (전체)
        반환 컬럼: Code, Name, Market, Marcap, Stocks, Sector? ...
        """
        mkt = market.upper()
        if mkt not in {"KOSPI", "KOSDAQ", "KONEX", "KRX"}:
            raise ValueError(f"unknown market: {market}")
        return self._fdr.StockListing(mkt)

    # ---- 시가총액 상위 N ----

    def top_n_by_market_cap(self, market: str, n: int = 200) -> list[Listing]:
        """시가총액 상위 N 종목.

        KOSPI 상위 200 ≈ KOSPI200 (공식 지수와 약간 차이),
        KOSDAQ 상위 100 ≈ "코스닥 우량 100선" 목적.
        """
        df = self.fetch_listed_companies(market)
        if "Marcap" not in df.columns:
            raise RuntimeError("FDR response missing 'Marcap' column")
        df = df.dropna(subset=["Marcap"]).sort_values("Marcap", ascending=False).head(n)

        sector_col = "Sector" if "Sector" in df.columns else None
        isin_col = "ISU_CD" if "ISU_CD" in df.columns else None
        market_col = "Market" if "Market" in df.columns else None

        results: list[Listing] = []
        for _, row in df.iterrows():
            results.append(Listing(
                stock_code=str(row["Code"]).zfill(6),
                name=str(row["Name"]),
                market=str(row[market_col]) if market_col else market.upper(),
                market_cap=int(row["Marcap"]) if row["Marcap"] else None,
                sector=str(row[sector_col]) if sector_col and row.get(sector_col) is not None else None,
                isin=str(row[isin_col]) if isin_col else None,
            ))
        return results
