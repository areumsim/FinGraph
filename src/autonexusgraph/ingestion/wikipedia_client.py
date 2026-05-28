"""Wikipedia REST API 클라이언트 — 한국어 페이지 본문 + Infobox.

엔드포인트:
  - REST summary:  https://ko.wikipedia.org/api/rest_v1/page/summary/<title>
  - HTML 본문:    https://ko.wikipedia.org/api/rest_v1/page/html/<title>
  - Action API:   https://ko.wikipedia.org/w/api.php?action=parse  (Infobox 추출용)

라이선스: CC BY-SA 4.0 — 본문 저장 OK, 출처 표기 의무.

전략:
- title 매칭: Wikidata 가 이미 매핑됐다면 sitelinks.kowiki.title 사용
- 없으면 corp_name 그대로 시도, 실패하면 search API fallback
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


WIKI_BASE_KO = "https://ko.wikipedia.org"
WIKI_BASE_EN = "https://en.wikipedia.org"

USER_AGENT = (
    "FinGraph-Research/0.1 "
    "(https://github.com/areumsim/FinGraph; ifkbn@kolon.com) "
    "Korean-finance GraphRAG ingestion bot"
)


@dataclass(frozen=True)
class WikiPage:
    title: str
    lang: str
    page_id: int | None
    revision_id: int | None
    extract: str | None          # summary text
    html: str | None             # 본문 (가져왔을 때만)
    infobox: dict | None         # parsed key-value
    last_modified: str | None
    raw_summary: dict | None


class WikipediaClient:
    """한국어/영어 Wikipedia REST API."""

    def __init__(self, lang: str = "ko", timeout: float = 30.0) -> None:
        self.lang = lang
        self.base = WIKI_BASE_KO if lang == "ko" else WIKI_BASE_EN
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    def __enter__(self) -> "WikipediaClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── summary (Action API — REST v1 보다 안정적) ───────────
    def get_summary(self, title: str) -> dict | None:
        """page summary via action=query (extracts).

        Action API 는 REST v1 보다 robust 하고 UA 제한이 덜 엄격.
        """
        url = f"{self.base}/w/api.php"
        params = {
            "action": "query",
            "prop": "extracts|info|pageprops",
            "exintro": "1", "explaintext": "1",
            "inprop": "url",
            "titles": title,
            "redirects": "1",
            "format": "json",
        }
        resp = self._client.get(url, params=params)
        if resp.status_code >= 400:
            resp.raise_for_status()
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        if not pages:
            return None
        # 첫 page (id=-1 이면 not found)
        page = next(iter(pages.values()))
        if str(page.get("pageid", "-1")) == "-1" or "missing" in page:
            return None
        return {
            "title": page.get("title"),
            "pageid": page.get("pageid"),
            "extract": page.get("extract"),
            "revision": page.get("lastrevid"),
            "timestamp": page.get("touched"),
            "fullurl": page.get("fullurl"),
            "pageprops": page.get("pageprops", {}),
        }

    # ── html 본문 (parse API — 본문 HTML) ─────────────────────
    def get_html(self, title: str) -> str | None:
        """Action API parse → 렌더된 HTML 본문."""
        url = f"{self.base}/w/api.php"
        params = {
            "action": "parse", "page": title, "prop": "text",
            "format": "json", "redirects": "1",
        }
        resp = self._client.get(url, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data.get("parse", {}).get("text", {}).get("*")

    # ── infobox (Action API parse) ───────────────────────────
    def get_infobox(self, title: str) -> dict | None:
        """{{Infobox 회사 ...}} 의 key-value 를 dict 으로.

        Action API parse 로 wikitext 받아 정규식 파싱.
        """
        url = f"{self.base}/w/api.php"
        params = {
            "action": "parse",
            "page": title,
            "prop": "wikitext",
            "format": "json",
            "redirects": "true",
        }
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        wikitext = data.get("parse", {}).get("wikitext", {}).get("*")
        if not wikitext:
            return None
        return _parse_infobox(wikitext)

    # ── search fallback ──────────────────────────────────────
    def search(self, query: str, limit: int = 5) -> list[dict]:
        url = f"{self.base}/w/api.php"
        params = {
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": limit, "format": "json",
        }
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json().get("query", {}).get("search", [])

    def fetch(self, title: str, *, with_html: bool = True,
              with_infobox: bool = True) -> WikiPage | None:
        """summary + (optionally) html + infobox 한 번에."""
        summary = self.get_summary(title)
        if not summary:
            return None
        html = self.get_html(title) if with_html else None
        infobox = self.get_infobox(title) if with_infobox else None
        return WikiPage(
            title=summary.get("title", title),
            lang=self.lang,
            page_id=summary.get("pageid"),
            revision_id=summary.get("revision"),
            extract=summary.get("extract"),
            html=html,
            infobox=infobox,
            last_modified=summary.get("timestamp"),
            raw_summary=summary,
        )


# ── Infobox 파서 ────────────────────────────────────────────
import re

_INFOBOX_HEAD = re.compile(r"\{\{\s*(Infobox|정보상자)\b", re.IGNORECASE)


def _parse_infobox(wikitext: str) -> dict | None:
    """{{Infobox 회사 | 키 = 값 ...}} 의 key-value 를 dict 으로.

    한국어 위키의 회사 정보상자 키:
        이름, 회사명, 영업 분야, 설립, 본사, 대표자, 매출액, 직원 수, 모기업, 자회사, 웹사이트
    """
    m = _INFOBOX_HEAD.search(wikitext)
    if not m:
        return None
    # 균형 잡힌 {{ }} 추출
    start = m.start()
    depth = 0
    i = start
    while i < len(wikitext):
        ch = wikitext[i]
        if ch == "{" and wikitext[i:i+2] == "{{":
            depth += 1
            i += 2
        elif ch == "}" and wikitext[i:i+2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                break
        else:
            i += 1
    block = wikitext[start:i]

    # | key = value (값에 | 가 있으면 깊이 추적)
    out: dict[str, str] = {}
    # 토큰 분리 — 중첩 {{}} 와 [[]] 안의 | 는 무시
    parts: list[str] = []
    cur = []
    nest = 0
    for ch in block:
        if ch == "{" or ch == "[":
            nest += 1
            cur.append(ch)
        elif ch == "}" or ch == "]":
            nest -= 1
            cur.append(ch)
        elif ch == "|" and nest <= 1:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))

    # 첫 part 는 "{{Infobox 회사" — 스킵
    for p in parts[1:]:
        if "=" not in p:
            continue
        k, _, v = p.partition("=")
        k = k.strip()
        v = v.strip().strip("{}").strip()
        if k:
            # wiki link [[...]] 정리
            v = re.sub(r"\[\[([^\|\]]+)(?:\|[^\]]+)?\]\]", r"\1", v)
            v = re.sub(r"<[^>]+>", "", v).strip()
            if v:
                out[k] = v[:1000]
    return out if out else None
