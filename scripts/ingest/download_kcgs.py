#!/usr/bin/env python3
"""KCGS (한국ESG기준원) 보도자료 모니터 + 등급 메타정보 수집.

KCGS 는 공식 API 가 없고, ESG 등급은 회원만 풀데이터 접근.
이 스크립트는 가능한 자동화:
1. 매년 등급 발표 시점 (보통 10~11월) 보도자료 페이지 polling
2. 'ESG 등급' / '등급' 키워드 매칭 보도자료 메타 + URL 자동 다운
3. 사용자에게 알람 출력 → 사용자가 페이지 방문해서 CSV 직접 다운

저장:
  data/raw/kcgs/press/<no>/meta.json  + body.html
  data/raw/kcgs/<year>/ratings.csv    ← 수동 다운로드 후 여기 둠
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.ingestion._common import (
    CheckpointStore, fetch_with_retry, get_rate_limiter, save_raw,
)
import httpx


PRESS_BASE = "https://www.cgs.or.kr/news/press_list.jsp"
PRESS_VIEW = "https://www.cgs.or.kr/news/press_view.jsp"

USER_AGENT = "Mozilla/5.0 (FinGraph-Research)"
ESG_KEYWORDS = ("등급", "ESG", "지배구조", "평가")


def _list_posts(svalue: str = "등급", limit_pages: int = 3) -> list[dict]:
    """KCGS 보도자료 — 키워드 검색 + 페이지네이션."""
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15)
    out: list[dict] = []
    seen_no: set[str] = set()
    for page in range(1, limit_pages + 1):
        params = {"svalue": svalue, "skey": "subject", "pg": page}
        r = client.get(PRESS_BASE, params=params)
        if r.status_code != 200:
            break
        # 게시물 no + title + date 추출 (KCGS HTML 패턴)
        # press_view.jsp?...no=XXX
        rows = re.findall(
            r'press_view\.jsp\?[^"\']*no=(\d+)[^"\']*"[^>]*>\s*(.*?)\s*</a>',
            r.text, re.DOTALL,
        )
        # 날짜는 같은 row 내 인접
        dates = re.findall(r'<td[^>]*class=["\']date["\'][^>]*>\s*(\d{4}\-\d{2}\-\d{2})\s*</td>', r.text)
        for i, (no, title) in enumerate(rows):
            if no in seen_no:
                continue
            seen_no.add(no)
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            out.append({
                "no": no,
                "title": title_clean,
                "date": dates[i] if i < len(dates) else None,
                "url": f"{PRESS_VIEW}?no={no}",
            })
    client.close()
    return out


def _fetch_post(no: str) -> dict:
    r = httpx.get(PRESS_VIEW, params={"no": no},
                  headers={"User-Agent": USER_AGENT}, timeout=15)
    r.raise_for_status()
    # 본문 영역 (KCGS HTML 패턴 — class 또는 id 로)
    body_m = re.search(r'(?:class|id)=["\'](?:view_cont|board_view_content|content)["\'][^>]*>(.*?)</(?:div|td)>',
                       r.text, re.DOTALL | re.IGNORECASE)
    body_html = body_m.group(1) if body_m else r.text[:5000]
    # 첨부파일 함수 호출 (KCGS 는 onclick="fileDownload(...)") — 함수 인자 추출만
    files = re.findall(r"(?:fileDown|download|fileDownload)\s*\(([^)]+)\)", r.text)
    return {"no": no, "body_html": body_html, "file_calls": files[:10]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--svalue", default="등급", help="검색 키워드")
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--with-body", action="store_true")
    args = parser.parse_args()

    print(f"[KCGS] 보도자료 검색 '{args.svalue}' (최근 {args.pages} pages)")
    posts = _list_posts(svalue=args.svalue, limit_pages=args.pages)
    print(f"[KCGS] matched: {len(posts)}")

    ckpt = CheckpointStore("kcgs_press")
    limiter = get_rate_limiter("kcgs")
    new_posts: list[dict] = []

    for p in posts:
        if ckpt.is_done(p["no"]):
            continue
        limiter.acquire()
        save_raw("kcgs", f"press/{p['no']}/meta.json", p)
        if args.with_body:
            try:
                limiter.acquire()
                body = fetch_with_retry(lambda no=p["no"]: _fetch_post(no), max_tries=3)
                save_raw("kcgs", f"press/{p['no']}/body.html", body.get("body_html", ""))
                if body.get("file_calls"):
                    save_raw("kcgs", f"press/{p['no']}/file_calls.json", body["file_calls"])
            except Exception as e:
                ckpt.mark_failed(p["no"], str(e))
                continue
        ckpt.mark_done(p["no"], {"title": p["title"], "date": p["date"]})
        new_posts.append(p)

    print(f"\n[KCGS] 신규 수집: {len(new_posts)}건")
    for p in new_posts[:15]:
        print(f"  [{p.get('date')}] {p['no']}: {p['title'][:80]}")

    # 등급/평가 키워드 매칭 시 사용자 액션 안내
    rating_posts = [p for p in new_posts
                    if any(k in p["title"] for k in ESG_KEYWORDS)]
    if rating_posts:
        print(f"\n[KCGS] ⚠️  ESG 등급 관련 보도자료 {len(rating_posts)}건:")
        for p in rating_posts:
            print(f"  • {p['date']} {p['title']}")
            print(f"    {p['url']}")
        print("\n→ 위 보도자료 페이지 방문해 등급표 CSV 다운로드 후:")
        print("   data/raw/kcgs/<year>/ratings.csv 에 저장")
        print("   python scripts/load/load_kcgs.py --year <year>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
