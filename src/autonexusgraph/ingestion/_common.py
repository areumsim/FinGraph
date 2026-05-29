"""ingestion 공통 인프라 — RateLimiter / 재시도 / Checkpoint / save_raw.

원칙 (사용자 명시):
- 원본 보존: 받은 그대로 data/raw/<source>/<key> 에 저장. 손대지 않음.
- 멱등: 같은 entity 다시 호출해도 같은 raw 파일.
- 천천히 안 터지게: rate-limit + exponential backoff + checkpoint resume.

사용:
    from autonexusgraph.ingestion._common import RateLimiter, CheckpointStore, save_raw, fetch_with_retry

    limiter = RateLimiter(per_sec=1.0)           # Wikidata 같이 느린 API
    ckpt = CheckpointStore("wikidata")
    for corp_code in targets:
        if ckpt.is_done(corp_code):
            continue
        limiter.acquire()
        try:
            payload = fetch_with_retry(lambda: client.fetch(corp_code))
            save_raw("wikidata", f"{corp_code}.json", payload)
            ckpt.mark_done(corp_code)
        except Exception as e:
            ckpt.mark_failed(corp_code, str(e))
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, TypeVar


log = logging.getLogger(__name__)

from ..config import get_settings
from ._license import allow_body


T = TypeVar("T")


# ─── Rate Limiter ─────────────────────────────────────────────────
class RateLimiter:
    """thread-safe 최소 간격 가드.

    per_sec=10 → 호출 사이 최소 0.1초. 동시 스레드도 안전(Lock).
    """

    def __init__(self, per_sec: float) -> None:
        if per_sec <= 0:
            self._min_interval = 0.0
        else:
            self._min_interval = 1.0 / per_sec
        self._last_call = 0.0
        self._lock = Lock()

    def acquire(self) -> None:
        if self._min_interval == 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last_call + self._min_interval - now
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()


# 소스별 권장 rate limit (env 로 override 가능)
DEFAULT_RATE_LIMITS: dict[str, float] = {
    "dart":         10.0,   # 일 10,000 콜 한도 — 여유롭게
    "krx":          5.0,
    "ecos":         5.0,
    "ftc":          3.0,
    "fss_press":    2.0,    # HTML 스크래핑 — 보수적
    "news_rss":     1.0,    # RSS 폴링 — 자주 칠 필요 없음
    "wikidata":     1.0,    # SPARQL — 무거운 쿼리 가능
    "wikipedia":    5.0,
    "kosis":        3.0,
    "kipris":       3.0,
    "kcgs":         2.0,
    "law":          5.0,
    "sec_edgar":    10.0,
    "gleif":        5.0,
    "data_go_kr":   3.0,
}


def get_rate_limiter(source: str) -> RateLimiter:
    """env override 또는 DEFAULT_RATE_LIMITS 에서 RateLimiter 생성."""
    env_key = f"INGEST_RATE_{source.upper()}_PER_SEC"
    override = os.environ.get(env_key)
    if override:
        try:
            return RateLimiter(float(override))
        except ValueError:
            pass
    return RateLimiter(DEFAULT_RATE_LIMITS.get(source, 5.0))


# ─── Retry ────────────────────────────────────────────────────────
def _retry_env(name: str, default: float) -> float:
    """INGEST_RETRY_{MAX,BASE,JITTER} env override — parsing 실패 시 default + log."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("[retry] %s=%r 파싱 실패 — default %s 사용", name, raw, default)
        return default


def fetch_with_retry(
    fn: Callable[[], T],
    max_tries: int | None = None,
    base: float | None = None,
    jitter: float | None = None,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    """exponential backoff + jitter.

    fn 이 raise 하면 base*2^(attempt-1) + uniform(0..jitter) 초 대기 후 재시도.
    max_tries 초과 시 마지막 예외 raise.

    env override (None 인 인자에만 적용):
        INGEST_RETRY_MAX     — max_tries (default 5)
        INGEST_RETRY_BASE    — base wait sec (default 2.0)
        INGEST_RETRY_JITTER  — jitter sec (default 0.3)
    """
    if max_tries is None:
        max_tries = int(_retry_env("INGEST_RETRY_MAX", 5))
    if base is None:
        base = _retry_env("INGEST_RETRY_BASE", 2.0)
    if jitter is None:
        jitter = _retry_env("INGEST_RETRY_JITTER", 0.3)

    last_exc: Exception | None = None
    for attempt in range(1, max_tries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt >= max_tries:
                break
            wait = base * (2 ** (attempt - 1)) + random.uniform(0, jitter)
            if on_retry:
                on_retry(attempt, e)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


# ─── Raw 저장 ─────────────────────────────────────────────────────
def raw_dir(source: str) -> Path:
    """data/raw/<source>/ — 없으면 생성."""
    root = get_settings().ingest_raw_dir / source
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_raw(
    source: str,
    rel_path: str,
    payload: Any,
    *,
    body_fields: tuple[str, ...] = ("body", "body_text", "body_html", "content", "fulltext"),
) -> Path:
    """payload 를 data/raw/<source>/<rel_path> 에 원자적 저장.

    - dict/list 면 JSON 으로, str 면 텍스트로, bytes 면 그대로.
    - 본문 저장이 금지된 source (copyrighted 등) 면 body 필드는 strip.
    - 임시파일에 쓴 뒤 rename — 부분쓰기 방지.

    Returns: 저장된 절대 경로.
    """
    target = raw_dir(source) / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(payload, dict) and not allow_body(source):
        # 본문 필드 제거 (메타·요약만 보존)
        payload = {k: v for k, v in payload.items() if k not in body_fields}

    tmp = target.with_suffix(target.suffix + ".tmp")
    if isinstance(payload, (dict, list)):
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    elif isinstance(payload, str):
        tmp.write_text(payload, encoding="utf-8")
    elif isinstance(payload, bytes):
        tmp.write_bytes(payload)
    else:
        # fallback — JSON 으로 시도
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)

    # POSIX rename 은 원자적 (같은 파일시스템 내)
    os.replace(tmp, target)
    return target


# ─── Checkpoint ───────────────────────────────────────────────────
@dataclass
class CheckpointStats:
    done: int = 0
    failed: int = 0
    skipped: int = 0


class CheckpointStore:
    """수집 진행 상태 — JSONL 기반, append-only.

    파일:
      data/state/ingest/<source>.done.jsonl    (entity_id 1줄씩)
      data/state/ingest/<source>.failed.jsonl  ({"id":..., "error":..., "at":...} 1줄씩)

    is_done() 은 메모리 set 으로 O(1). 첫 호출 시 파일 read.
    mark_done()·mark_failed() 는 append (flush 후 fsync 로 내구성 확보).
    """

    def __init__(self, source: str, state_root: Path | None = None) -> None:
        self.source = source
        root = state_root or (get_settings().ingest_raw_dir.parent / "state" / "ingest")
        self.done_path = root / f"{source}.done.jsonl"
        self.failed_path = root / f"{source}.failed.jsonl"
        # source 에 '/' 가 포함된 경우 (예: 'auto/nhtsa_vpic') 도 안전하게.
        self.done_path.parent.mkdir(parents=True, exist_ok=True)
        self._done: set[str] = self._load_done()
        self.stats = CheckpointStats()

    def _load_done(self) -> set[str]:
        if not self.done_path.exists():
            return set()
        s: set[str] = set()
        with self.done_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    s.add(str(obj["id"]))
                except (json.JSONDecodeError, KeyError):
                    continue
        return s

    def is_done(self, entity_id: str) -> bool:
        return str(entity_id) in self._done

    def mark_done(self, entity_id: str, meta: dict | None = None) -> None:
        entity_id = str(entity_id)
        if entity_id in self._done:
            return
        record = {"id": entity_id, "at": time.time()}
        if meta:
            record.update(meta)
        with self.done_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._done.add(entity_id)
        self.stats.done += 1

    def mark_failed(self, entity_id: str, error: str, meta: dict | None = None) -> None:
        record = {"id": str(entity_id), "error": str(error)[:500], "at": time.time()}
        if meta:
            record.update(meta)
        with self.failed_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        self.stats.failed += 1

    def mark_skipped(self) -> None:
        self.stats.skipped += 1

    def iter_failed(self) -> list[dict]:
        """failed.jsonl 의 마지막 실패 시도만 (id 별 dedup)."""
        if not self.failed_path.exists():
            return []
        latest: dict[str, dict] = {}
        with self.failed_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    latest[str(obj["id"])] = obj
                except (json.JSONDecodeError, KeyError):
                    continue
        return list(latest.values())

    def reset(self) -> None:
        """state 초기화 — --force 옵션에서 사용."""
        if self.done_path.exists():
            self.done_path.unlink()
        if self.failed_path.exists():
            self.failed_path.unlink()
        self._done.clear()
        self.stats = CheckpointStats()


# ─── 텍스트 정규화 헬퍼 ──────────────────────────────────────────

# 영문 법인격 token — word boundary 매칭만 (substring 매칭 X).
# 옛 .replace() 방식은 'Transit Connect' → 'Transit nnect' (Connect 의 Co)
# 같은 over-matching 버그.
import re as _re_norm

_EN_LEGAL_RE = _re_norm.compile(
    r"\b(?:Inc|Ltd|Co|Corp|Corporation|Company|Limited)\b\.?",
    _re_norm.IGNORECASE,
)
# 한글 법인격 — 단어 경계 개념 부적합, 그대로 substring replace.
_KO_LEGAL = (
    "(주)", "㈜", "주식회사",
    "(유)", "유한회사",
    "(합)", "합자회사",
)


def normalize_corp_name(name: str) -> str:
    """회사명 표준화 — 비교/매칭용. SSOT 는 아님(원본 보존).

    예: '(주)삼성전자' / '㈜삼성전자' / '주식회사 삼성전자' / 'Samsung Electronics Inc.'
        → '삼성전자' / 'samsung electronics'

    버그 회피: 'Transit Connect' (Connect 안의 Co), 'Cordova Sedan' (Cordova 의 Co) 등
    영문 token 은 ``\\b`` word boundary 매칭만.
    """
    if not name:
        return ""
    s = name.strip()
    for t in _KO_LEGAL:
        s = s.replace(t, " ")
    s = _EN_LEGAL_RE.sub(" ", s)
    s = " ".join(s.split())   # 공백 정규화
    return s.lower()
