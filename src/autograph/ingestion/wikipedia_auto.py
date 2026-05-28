"""Wikipedia (ko/en) 자동차 본문 ingestion — 키 불필요.

대상:
    auto.master_vehicle_models.name  (예: 'Sonata', 'Grandeur', 'Model Y')
    auto.master_manufacturers.name   (예: 'Hyundai', 'Tesla', 'Genesis')

전략:
    1) Wikidata QID 가 있으면 sitelinks 로 정확 title 획득 (가장 신뢰).
    2) 없으면 name 직접 시도 (Wikipedia 가 redirect 해결).
    3) ko 우선, 실패 시 en fallback.
    4) summary + html + infobox 한 번에 fetch — 본문 청크에 사용.

저장:
    data/raw/auto/wikipedia/{LANG}/models/{model_id}.json
    data/raw/auto/wikipedia/{LANG}/manufacturers/{manufacturer_id}.json

retrieve.py 의 ``AUTO_SOURCES`` 에 등장하는 ``wikipedia_auto`` source 가 본 모듈이
producer. build_chunks_auto.build_from_wikipedia() 가 청크로 변환.

CLI:
    python -m autograph.ingestion.wikipedia_auto --models
    python -m autograph.ingestion.wikipedia_auto --manufacturers
    python -m autograph.ingestion.wikipedia_auto --all
    python -m autograph.ingestion.wikipedia_auto --models --lang en
    python -m autograph.ingestion.wikipedia_auto --models --limit 30
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from typing import Any

from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import (
    CheckpointStore,
    RateLimiter,
    save_raw,
)
from autonexusgraph.ingestion.wikipedia_client import WikipediaClient


log = logging.getLogger(__name__)


_SOURCE = "auto/wikipedia"
# 한국·영문 위키 보수적 1.5 req/sec — 본문까지 받으므로 무거움.
_LIMITER = RateLimiter(per_sec=1.5)


def _title_from_qid(qid: str, lang: str) -> str | None:
    """Wikidata sitelinks 에서 {lang}wiki title 추출. 실패 시 None.

    https://www.wikidata.org/wiki/Special:EntityData/{qid}.json
    """
    import httpx
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    try:
        with httpx.Client(timeout=15.0,
                          headers={"User-Agent": "AutoGraph-Research/0.1"}) as c:
            r = c.get(url)
            if r.status_code != 200:
                return None
            data = r.json()
    except Exception:   # noqa: BLE001
        return None
    entity = (data.get("entities") or {}).get(qid)
    if not entity:
        return None
    sitelinks = entity.get("sitelinks") or {}
    sl = sitelinks.get(f"{lang}wiki")
    if isinstance(sl, dict):
        return sl.get("title")
    return None


def _fetch_entity_pages(
    *,
    entity_kind: str,           # 'models' | 'manufacturers'
    rows: list[tuple],          # (id, name, wikidata_qid)
    lang: str,
    with_html: bool,
    with_infobox: bool,
) -> dict[str, int]:
    """rows 각각에 대해 Wikipedia 조회 + raw 저장. dict 통계 반환."""
    ckpt = CheckpointStore(f"{_SOURCE}/{lang}/{entity_kind}")
    stats = {"fetched": 0, "skipped": 0, "missing": 0, "errors": 0}

    with WikipediaClient(lang=lang) as wiki:
        for eid, name, qid in rows:
            key = f"{lang}|{entity_kind}|{eid}"
            if ckpt.is_done(key):
                stats["skipped"] += 1
                ckpt.mark_skipped()
                continue

            # title 결정 — QID 우선, fallback 으로 name.
            title: str | None = None
            if qid:
                _LIMITER.acquire()
                try:
                    title = _title_from_qid(qid, lang)
                except Exception as e:   # noqa: BLE001
                    log.debug("[wiki:%s] qid->title 실패 %s: %s", lang, qid, e)
            if not title:
                title = name
            if not title:
                stats["missing"] += 1
                continue

            _LIMITER.acquire()
            try:
                page = wiki.fetch(title, with_html=with_html,
                                  with_infobox=with_infobox)
            except Exception as e:   # noqa: BLE001
                log.warning("[wiki:%s] fetch %s 실패: %s", lang, title, e)
                stats["errors"] += 1
                ckpt.mark_failed(key, str(e))
                continue

            if not page or not page.extract:
                # 미존재 → search fallback 한 번.
                try:
                    hits = wiki.search(name or title, limit=1)
                    if hits:
                        alt = hits[0].get("title")
                        if alt and alt != title:
                            _LIMITER.acquire()
                            page = wiki.fetch(alt, with_html=with_html,
                                              with_infobox=with_infobox)
                except Exception as e:   # noqa: BLE001
                    log.debug("[wiki:%s] search %s 실패: %s", lang, name, e)

            if not page or not page.extract:
                stats["missing"] += 1
                ckpt.mark_done(key, {"missing": True, "title": title})
                continue

            # 저장 — dataclass 를 dict 으로.
            payload: dict[str, Any] = dataclasses.asdict(page)
            # html 은 대용량 — 길면 본문만 저장. infobox/extract 가 핵심.
            if payload.get("html") and len(payload["html"]) > 200_000:
                payload["html"] = payload["html"][:200_000] + "...<TRUNCATED>"
            payload["__entity"] = {"kind": entity_kind, "id": eid, "name": name,
                                    "qid": qid}
            rel = f"{lang}/{entity_kind}/{eid}.json"
            try:
                save_raw(_SOURCE, rel, payload)
                stats["fetched"] += 1
                ckpt.mark_done(key, {"title": page.title,
                                     "extract_len": len(page.extract or "")})
                log.info("[wiki:%s] %s [%s] %s → %d chars",
                         lang, entity_kind, eid, page.title,
                         len(page.extract or ""))
            except Exception as e:   # noqa: BLE001
                log.warning("[wiki:%s] save %s 실패: %s", lang, key, e)
                stats["errors"] += 1
                ckpt.mark_failed(key, str(e))

    return stats


def _load_models_from_pg(limit: int | None) -> list[tuple]:
    conn = get_connection()
    with conn.cursor() as cur:
        q = """
            SELECT model_id, name, wikidata_qid
              FROM auto.master_vehicle_models
             ORDER BY model_id
        """
        if limit:
            q += f" LIMIT {int(limit)}"
        cur.execute(q)
        return list(cur.fetchall())


def _load_manufacturers_from_pg(limit: int | None) -> list[tuple]:
    conn = get_connection()
    with conn.cursor() as cur:
        q = """
            SELECT manufacturer_id, name, wikidata_qid
              FROM auto.master_manufacturers
             ORDER BY manufacturer_id
        """
        if limit:
            q += f" LIMIT {int(limit)}"
        cur.execute(q)
        return list(cur.fetchall())


def ingest(
    *,
    targets: tuple[str, ...] = ("models", "manufacturers"),
    lang: str = "ko",
    with_html: bool = True,
    with_infobox: bool = True,
    limit: int | None = None,
    fallback_lang: str | None = "en",
) -> dict[str, dict[str, int]]:
    """전체 또는 일부 entity 의 wikipedia 본문 수집.

    fallback_lang 가 주어지면 1차 lang 에서 미발견인 entity 만 fallback 으로 재시도.
    """
    out: dict[str, dict[str, int]] = {}
    for tgt in targets:
        rows = (_load_models_from_pg(limit) if tgt == "models"
                else _load_manufacturers_from_pg(limit))
        if not rows:
            log.warning("[wiki] %s PG 비어있음 — vpic/wikidata 적재 선행 필요", tgt)
            out[f"{lang}/{tgt}"] = {"fetched": 0, "skipped": 0,
                                     "missing": 0, "errors": 0}
            continue
        log.info("[wiki] %s — %d entities", tgt, len(rows))
        stats = _fetch_entity_pages(
            entity_kind=tgt, rows=rows, lang=lang,
            with_html=with_html, with_infobox=with_infobox,
        )
        out[f"{lang}/{tgt}"] = stats

        # 미발견 entity 만 fallback_lang 으로 재시도.
        if fallback_lang and fallback_lang != lang and stats["missing"]:
            log.info("[wiki:%s→%s] %d missing entities 재시도",
                     lang, fallback_lang, stats["missing"])
            stats_fb = _fetch_entity_pages(
                entity_kind=tgt, rows=rows, lang=fallback_lang,
                with_html=with_html, with_infobox=with_infobox,
            )
            out[f"{fallback_lang}/{tgt}"] = stats_fb

    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.wikipedia_auto")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--models", action="store_true",
                      help="auto.master_vehicle_models 본문 수집")
    grp.add_argument("--manufacturers", action="store_true",
                      help="auto.master_manufacturers 본문 수집")
    grp.add_argument("--all", action="store_true",
                      help="models + manufacturers 모두")
    ap.add_argument("--lang", default="ko", choices=["ko", "en"])
    ap.add_argument("--fallback-lang", default="en",
                    help="1차 미발견 시 재시도 언어. 'none' 으로 비활성.")
    ap.add_argument("--no-html", action="store_true",
                    help="HTML 본문 skip (summary + infobox 만)")
    ap.add_argument("--no-infobox", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="PG 에서 최대 N entity (smoke test)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.all:
        targets: tuple[str, ...] = ("models", "manufacturers")
    elif args.models:
        targets = ("models",)
    elif args.manufacturers:
        targets = ("manufacturers",)
    else:
        ap.error("--models / --manufacturers / --all 중 하나 필요")

    fb = None if args.fallback_lang.lower() == "none" else args.fallback_lang
    stats = ingest(
        targets=targets,
        lang=args.lang,
        with_html=not args.no_html,
        with_infobox=not args.no_infobox,
        limit=args.limit,
        fallback_lang=fb,
    )
    log.info("[wiki] done: %s", json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()


__all__ = ["ingest", "_fetch_entity_pages", "_title_from_qid"]
