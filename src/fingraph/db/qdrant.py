"""Qdrant 클라이언트 헬퍼.

PRD §4.3 — Qdrant는 의미·서술형 청크 벡터 저장소.
"""

from __future__ import annotations

from functools import lru_cache

from ..config import get_settings


@lru_cache(maxsize=1)
def get_client():
    """QdrantClient 싱글톤. qdrant-client 패키지 필요 (pip install '.[db]')."""
    from qdrant_client import QdrantClient

    s = get_settings()
    if s.qdrant_api_key:
        return QdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key)
    return QdrantClient(url=s.qdrant_url)


def ping() -> bool:
    """연결 헬스체크 — 컬렉션 리스트 호출."""
    try:
        client = get_client()
        client.get_collections()
        return True
    except Exception:
        return False
