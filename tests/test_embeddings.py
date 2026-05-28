"""임베딩 클라이언트 unit 테스트 — HTTP mock."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_embed_returns_vectors():
    from autonexusgraph.embeddings import EmbeddingClient

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    with patch("autonexusgraph.embeddings.httpx.Client") as mock_httpx:
        instance = MagicMock()
        instance.post.return_value = fake_resp
        mock_httpx.return_value = instance

        client = EmbeddingClient(embed_url="http://emb", rerank_url="http://rr")
        vectors = client.embed(["a", "b"])
        assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_embed_empty_short_circuits():
    from autonexusgraph.embeddings import EmbeddingClient

    with patch("autonexusgraph.embeddings.httpx.Client"):
        client = EmbeddingClient()
        assert client.embed([]) == []


def test_safe_embed_returns_none_rows_on_failure():
    from autonexusgraph.embeddings import EmbeddingClient, EmbeddingError

    with patch("autonexusgraph.embeddings.httpx.Client") as mock_httpx:
        instance = MagicMock()
        # post 가 예외 발생시키게
        instance.post.side_effect = Exception("connection refused")
        mock_httpx.return_value = instance

        client = EmbeddingClient()
        # safe_embed 는 EmbeddingError 를 잡고 None row 로 반환
        with patch.object(client, "embed", side_effect=EmbeddingError("nope")):
            results = client.safe_embed(["a", "b"])
        assert len(results) == 2
        assert all(r.vector is None for r in results)
        assert all(r.error == "nope" for r in results)


def test_rerank_parses_scores():
    from autonexusgraph.embeddings import EmbeddingClient

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = [
        {"index": 2, "score": 0.9},
        {"index": 0, "score": 0.5},
        {"index": 1, "score": 0.1},
    ]

    with patch("autonexusgraph.embeddings.httpx.Client") as mock_httpx:
        instance = MagicMock()
        instance.post.return_value = fake_resp
        mock_httpx.return_value = instance

        client = EmbeddingClient()
        results = client.rerank("q", ["a", "b", "c"], top_k=2)
        assert len(results) == 2
        assert results[0].index == 2
        assert results[0].score == pytest.approx(0.9)


def test_health_handles_endpoint_down():
    from autonexusgraph.embeddings import EmbeddingClient

    with patch("autonexusgraph.embeddings.httpx.Client") as mock_httpx:
        instance = MagicMock()
        instance.get.side_effect = Exception("no")
        mock_httpx.return_value = instance

        client = EmbeddingClient()
        h = client.health()
        assert h == {"embed": False, "rerank": False}
