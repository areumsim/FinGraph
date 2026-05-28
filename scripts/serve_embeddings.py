"""BGE-M3 + Reranker 를 dev container 안에서 직접 띄우는 FastAPI 서버.

배경:
- 현 dev container (ar-poc-dev-docker) 는 docker-in-docker 미지원
  → TEI 같은 별도 GPU 컨테이너 띄울 수 없음
- 다행히 dev container 가 GPU (1, 3) + 64GB RAM 보유
  → sentence-transformers 로 모델 직접 로드 + uvicorn 으로 HTTP 노출이 가장 단순

호환성:
- TEI 의 /embed, /rerank, /health 엔드포인트와 동일 schema → src/autonexusgraph/embeddings.py 그대로 사용

사용:
    # 의존성 (최초 1회)
    pip install sentence-transformers fastapi 'uvicorn[standard]' torch

    # GPU 1, 3 사용 (dev container 환경변수 이미 CUDA_VISIBLE_DEVICES=1,3)
    python scripts/serve_embeddings.py --embed-port 8080 --rerank-port 8081

    # 또는 임베딩만
    python scripts/serve_embeddings.py --embed-port 8080 --no-rerank

자원:
- BGE-M3 dense:   GPU ~2.5GB, RAM ~2GB
- BGE-Reranker-v2-m3: GPU ~1.5GB
- 단일 GPU 에 둘 다 올려도 4GB 면 충분 (V100/A100 어디든 OK)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("serve_embeddings")


def _make_embed_app(model_name: str, device: str):
    """BGE-M3 dense 임베딩 FastAPI 앱."""
    from fastapi import Body, FastAPI, HTTPException
    from sentence_transformers import SentenceTransformer

    log.info(f"loading embed model: {model_name} on {device} ...")
    model = SentenceTransformer(model_name, device=device)
    log.info(f"loaded embed model. dim={model.get_sentence_embedding_dimension()}")

    app = FastAPI(title=f"FinGraph embed ({model_name})")

    @app.get("/health")
    def health():
        return {"status": "ok", "model": model_name, "device": device,
                "dim": model.get_sentence_embedding_dimension()}

    @app.post("/embed")
    def embed(req: dict = Body(...)):
        inputs = req.get("inputs") or []
        if not inputs:
            return []
        normalize = bool(req.get("normalize", True))
        try:
            vecs = model.encode(inputs, normalize_embeddings=normalize,
                                convert_to_numpy=True, batch_size=32,
                                show_progress_bar=False)
            return vecs.tolist()
        except Exception as e:
            raise HTTPException(500, str(e)) from e

    return app


def _make_rerank_app(model_name: str, device: str):
    """BGE-Reranker FastAPI 앱."""
    from fastapi import Body, FastAPI, HTTPException
    from sentence_transformers import CrossEncoder

    log.info(f"loading rerank model: {model_name} on {device} ...")
    model = CrossEncoder(model_name, device=device)
    log.info("loaded rerank model")

    app = FastAPI(title=f"FinGraph rerank ({model_name})")

    @app.get("/health")
    def health():
        return {"status": "ok", "model": model_name, "device": device}

    @app.post("/rerank")
    def rerank(req: dict = Body(...)):
        query = req.get("query") or ""
        texts = req.get("texts") or []
        if not texts:
            return []
        try:
            pairs = [[query, t] for t in texts]
            scores = model.predict(pairs)
            ranked = sorted(
                [{"index": i, "score": float(s)} for i, s in enumerate(scores)],
                key=lambda x: x["score"], reverse=True,
            )
            return ranked
        except Exception as e:
            raise HTTPException(500, str(e)) from e

    return app


def _serve(app: Any, host: str, port: int) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> int:
    parser = argparse.ArgumentParser(description="FinGraph 임베딩/재정렬 서버 (TEI 호환 HTTP)")
    parser.add_argument("--embed-model", default="BAAI/bge-m3")
    parser.add_argument("--rerank-model", default="BAAI/bge-reranker-v2-m3")
    parser.add_argument("--embed-port", type=int, default=8080)
    parser.add_argument("--rerank-port", type=int, default=8081)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--device", default="cuda",
                        help="cuda | cuda:0 | cpu (기본 cuda — dev container 의 CUDA_VISIBLE_DEVICES 따름)")
    parser.add_argument("--no-rerank", action="store_true",
                        help="reranker 미기동 (임베딩만)")
    parser.add_argument("--no-embed", action="store_true",
                        help="embedding 미기동 (reranker 만)")
    args = parser.parse_args()

    # 모델 다운로드 위치 (dev container 의 .cache mount 활용)
    os.environ.setdefault("HF_HOME", os.path.expanduser("~/.cache/huggingface"))

    threads: list[threading.Thread] = []

    if not args.no_embed:
        embed_app = _make_embed_app(args.embed_model, args.device)
        t = threading.Thread(
            target=_serve, args=(embed_app, args.host, args.embed_port),
            daemon=True, name="embed-server",
        )
        t.start()
        threads.append(t)
        log.info(f"embed server  → http://{args.host}:{args.embed_port}")

    if not args.no_rerank:
        rerank_app = _make_rerank_app(args.rerank_model, args.device)
        t = threading.Thread(
            target=_serve, args=(rerank_app, args.host, args.rerank_port),
            daemon=True, name="rerank-server",
        )
        t.start()
        threads.append(t)
        log.info(f"rerank server → http://{args.host}:{args.rerank_port}")

    if not threads:
        log.error("--no-embed 와 --no-rerank 모두 켤 수 없음")
        return 2

    log.info("Ctrl+C 로 종료")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log.info("종료")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
