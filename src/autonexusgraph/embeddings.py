"""임베딩 / 재정렬 클라이언트.

PRD §5 (LLM 어댑터) 와 같은 정신으로, 임베딩도 추상화한다.
주 경로는 자체 호스팅 BGE-M3 (HuggingFace TEI 컨테이너).
TEI 미가동 환경에선 OpenAI fallback (선택).

설계 메모: 이전 v2 시스템의 라우팅·safe_embed 패턴을 흡수.
- Provider 분기 (env 기반)
- safe_embed: 호출 실패 시 None 반환 (파이프라인 중단 방지)
- 배치 임베딩 지원
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import get_settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbedResult:
    """단일 임베딩 결과."""

    vector: list[float] | None
    dim: int
    model: str
    error: str | None = None


@dataclass(frozen=True)
class RerankResult:
    """재정렬 결과 1행 — 원래 인덱스 + 점수."""

    index: int
    score: float


class EmbeddingClient:
    """BGE-M3 (TEI) HTTP 클라이언트.

    TEI 엔드포인트:
        POST /embed          { "inputs": ["text", ...] } → [[float], ...]
        POST /rerank         { "query": "...", "texts": [...] } → [{"index":i,"score":s}, ...]
    """

    def __init__(
        self,
        embed_url: str | None = None,
        rerank_url: str | None = None,
        model: str = "BAAI/bge-m3",
        timeout: float = 30.0,
    ) -> None:
        s = get_settings()
        self.embed_url = (embed_url or s.embedding_url).rstrip("/")
        self.rerank_url = (rerank_url or s.reranker_url).rstrip("/")
        self.model = model
        self.dim = s.embedding_dim
        self._client = httpx.Client(timeout=timeout)

    def __enter__(self) -> "EmbeddingClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---- 임베딩 ----

    def embed(self, texts: list[str]) -> list[list[float]]:
        """텍스트 배치 → 벡터 배치. 실패 시 예외."""
        if not texts:
            return []
        try:
            resp = self._client.post(f"{self.embed_url}/embed", json={"inputs": texts})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise EmbeddingError(f"embed failed ({self.embed_url}): {e}") from e
        data = resp.json()
        if not isinstance(data, list):
            raise EmbeddingError(f"unexpected embed response shape: {type(data)}")
        return data

    def embed_one(self, text: str) -> list[float]:
        """단일 텍스트 임베딩."""
        return self.embed([text])[0]

    def safe_embed(self, texts: list[str]) -> list[EmbedResult]:
        """배치 임베딩, 실패 시 row 별 None 반환 (파이프라인 friendly).

        이전 v2 의 safe_embed 패턴 흡수 — 실패 1건이 배치 전체를 끊지 못하게.
        """
        if not texts:
            return []
        try:
            vectors = self.embed(texts)
        except EmbeddingError as e:
            logger.warning("safe_embed: batch failed, returning None rows. %s", e)
            return [EmbedResult(vector=None, dim=self.dim, model=self.model, error=str(e))
                    for _ in texts]
        return [
            EmbedResult(vector=v, dim=len(v), model=self.model)
            for v in vectors
        ]

    # ---- 재정렬 ----

    def rerank(self, query: str, texts: list[str], top_k: int | None = None) -> list[RerankResult]:
        """BGE-Reranker — query 와 candidate 들의 관련도 점수 + 정렬."""
        if not texts:
            return []
        payload: dict[str, Any] = {"query": query, "texts": texts, "raw_scores": False}
        try:
            resp = self._client.post(f"{self.rerank_url}/rerank", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise EmbeddingError(f"rerank failed ({self.rerank_url}): {e}") from e
        data = resp.json()
        results = [RerankResult(index=int(r["index"]), score=float(r["score"])) for r in data]
        # TEI rerank 는 이미 score 내림차순. top_k 만 자름.
        if top_k:
            results = results[:top_k]
        return results

    # ---- 헬스 ----

    def health(self) -> dict[str, bool]:
        """embed/rerank 엔드포인트 가동 여부."""
        out = {"embed": False, "rerank": False}
        try:
            r = self._client.get(f"{self.embed_url}/health", timeout=5)
            out["embed"] = r.status_code == 200
        except Exception:
            pass
        try:
            r = self._client.get(f"{self.rerank_url}/health", timeout=5)
            out["rerank"] = r.status_code == 200
        except Exception:
            pass
        return out


class EmbeddingError(Exception):
    """임베딩/재정렬 호출 실패."""


# ── 싱글톤 ──────────────────────────────────────────────────────────
_singleton: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    global _singleton
    if _singleton is None:
        _singleton = EmbeddingClient()
    return _singleton
