"""인프라 헬스체크.

사용:
    python scripts/healthcheck.py
    python scripts/healthcheck.py --only neo4j,postgres

`make up` 후 모든 컴포넌트가 살아 있는지 한 번에 확인.
미설치 의존성은 SKIP, 연결 실패는 FAIL 로 구분.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.config import get_settings  # noqa: E402


def _check_neo4j() -> tuple[str, str]:
    try:
        from autonexusgraph.db import neo4j as nx
        return ("OK", "ping ok") if nx.ping() else ("FAIL", "ping returned False")
    except ImportError as e:
        return ("SKIP", f"neo4j package missing: {e}")
    except Exception as e:
        return ("FAIL", f"{type(e).__name__}: {e}")


def _check_postgres() -> tuple[str, str]:
    try:
        from autonexusgraph.db import postgres as pg
        return ("OK", "ping ok") if pg.ping() else ("FAIL", "ping returned False")
    except ImportError as e:
        return ("SKIP", f"psycopg missing: {e}")
    except Exception as e:
        return ("FAIL", f"{type(e).__name__}: {e}")


def _check_qdrant() -> tuple[str, str]:
    """옵션 — QDRANT_URL 미설정이면 SKIP."""
    from autonexusgraph.config import get_settings
    if not get_settings().qdrant_url:
        return ("SKIP", "QDRANT_URL 미설정 (minimal 스택 — pgvector 사용)")
    try:
        from autonexusgraph.db import qdrant as qd
        return ("OK", "ping ok") if qd.ping() else ("FAIL", "ping returned False")
    except ImportError as e:
        return ("SKIP", f"qdrant-client missing: {e}")
    except Exception as e:
        return ("FAIL", f"{type(e).__name__}: {e}")


def _check_pgvector() -> tuple[str, str]:
    """PG 의 vector 확장 활성화 확인."""
    try:
        from autonexusgraph.db import postgres as pg
        conn = pg.get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT extname FROM pg_extension WHERE extname='vector'")
            row = cur.fetchone()
            if row:
                return ("OK", "vector extension installed")
            return ("FAIL", "vector extension not installed — 01_schema.sql 적용 확인")
    except ImportError as e:
        return ("SKIP", f"psycopg missing: {e}")
    except Exception as e:
        return ("FAIL", f"{type(e).__name__}: {e}")


def _check_embedding() -> tuple[str, str]:
    try:
        from autonexusgraph.embeddings import get_embedding_client
        h = get_embedding_client().health()
        if h.get("embed") and h.get("rerank"):
            return ("OK", "embed+rerank up")
        if h.get("embed") or h.get("rerank"):
            return ("PARTIAL", str(h))
        return ("FAIL", "neither endpoint responding")
    except Exception as e:
        return ("FAIL", f"{type(e).__name__}: {e}")


def _check_dart() -> tuple[str, str]:
    s = get_settings()
    if not s.dart_api_key:
        return ("SKIP", "DART_API_KEY 미설정")
    try:
        import httpx
        # 가벼운 호출 — corp_code 한 건 (삼성전자: 00126380)
        r = httpx.get(
            f"{s.dart_base_url}/company.json",
            params={"crtfc_key": s.dart_api_key, "corp_code": "00126380"},
            timeout=15,
        )
        data = r.json()
        status = data.get("status", "?")
        if status == "000":
            return ("OK", f"company.json status=000 ({data.get('corp_name','?')})")
        return ("FAIL", f"DART status={status} message={data.get('message','')}")
    except Exception as e:
        return ("FAIL", f"{type(e).__name__}: {e}")


def _check_ecos() -> tuple[str, str]:
    s = get_settings()
    if not s.ecos_api_key:
        return ("SKIP", "ECOS_API_KEY 미설정")
    try:
        import httpx
        # 한국은행 기준금리 1포인트
        url = f"{s.ecos_base_url}/StatisticSearch/{s.ecos_api_key}/json/kr/1/1/722Y001/D/20240101/20240131/0101000"
        r = httpx.get(url, timeout=15)
        data = r.json()
        if "RESULT" in data:
            return ("FAIL", f"ECOS error: {data['RESULT']}")
        if "StatisticSearch" in data:
            return ("OK", "ECOS responding")
        return ("FAIL", f"unexpected: {str(data)[:120]}")
    except Exception as e:
        return ("FAIL", f"{type(e).__name__}: {e}")


CHECKS = {
    "neo4j":     _check_neo4j,
    "postgres":  _check_postgres,
    "pgvector":  _check_pgvector,
    "qdrant":    _check_qdrant,
    "embedding": _check_embedding,
    "dart":      _check_dart,
    "ecos":      _check_ecos,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="FinGraph 인프라 헬스체크")
    parser.add_argument("--only", type=str, default=None,
                        help=f"검사할 항목 (쉼표). 가능: {','.join(CHECKS)}")
    args = parser.parse_args()

    if args.only:
        names = [n.strip() for n in args.only.split(",") if n.strip() in CHECKS]
    else:
        names = list(CHECKS)

    print(f"{'COMPONENT':12s}  {'STATUS':8s}  DETAIL")
    print("-" * 80)
    fail = 0
    for name in names:
        try:
            status, detail = CHECKS[name]()
        except Exception as e:
            status, detail = "FAIL", f"checker crashed: {e}"
        line = f"{name:12s}  {status:8s}  {detail}"
        print(line)
        if status == "FAIL":
            fail += 1

    print("-" * 80)
    print(f"{'FAILED' if fail else 'ALL OK'} (fail={fail})")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
