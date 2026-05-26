"""금융감독원 (FSS) 보도자료·제재정보 크롤러.

라이선스: 공공누리 제1유형. 보도자료는 자유 이용. 제재정보는 출처표시.

소스:
1. 보도자료 RSS: https://www.fss.or.kr/fss/main/rss.do?bbsId=...
2. 보도자료 게시판 HTML
3. 제재정보 공개 (https://www.fss.or.kr/disclosure/cscndlist.do)

ar-poc-dev 환경에서 한국 IP 로 호출 (FSS 가 일부 페이지 IP 제한 가능).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx


FSS_BASE = "https://www.fss.or.kr"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class FssArticle:
    """FSS 보도자료 1건."""

    article_id: str            # bbsId 또는 nttId
    title: str
    published_at: str          # YYYY-MM-DD
    category: str | None       # 분류 (검사·감독/제재/...)
    summary: str | None
    body_html: str | None      # 본문 HTML (선택 — 가져올 때만)
    attachment_urls: list[str] # 첨부 PDF/HWP
    source_url: str


class FssClient:
    """FSS 보도자료 목록·본문 수집."""

    def __init__(self, base_url: str = FSS_BASE, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})

    def __enter__(self) -> "FssClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def list_press_releases(
        self,
        bbs_id: str = "BBS_0001",     # 보도자료 게시판 ID
        cms_cd: str = "0007",          # CMS 코드
        page: int = 1,
        size: int = 20,
        date_from: str | None = None,  # YYYY-MM-DD
        date_to: str | None = None,
    ) -> list[FssArticle]:
        """FSS 보도자료 목록.

        주의: FSS 사이트 구조가 바뀌면 selector 도 업데이트 필요.
        본 구현은 일반적인 한국 정부 게시판 패턴 — 첫 호출 시 검증 필요.
        """
        from bs4 import BeautifulSoup

        url = f"{self.base_url}/fss/main/list.do"
        params = {
            "bbsId": bbs_id, "cmsCd": cms_cd, "pageIndex": page,
            "pageUnit": size,
        }
        if date_from:
            params["searchStrtDate"] = date_from.replace("-", "")
        if date_to:
            params["searchEndDate"] = date_to.replace("-", "")

        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        results: list[FssArticle] = []
        # 표 또는 ul 패턴 — 두 가지 모두 시도
        rows = soup.select("table tbody tr") or soup.select("ul.board-list li")
        for row in rows:
            a = row.find("a")
            if not a:
                continue
            href = a.get("href", "")
            title = a.get_text(strip=True)
            # 날짜 — td 또는 span 안
            date_el = row.select_one("td.date, .date, .reg-date")
            published = date_el.get_text(strip=True) if date_el else ""
            # nttId 또는 article id 추출
            import re
            m = re.search(r"nttId=(\d+)", href)
            article_id = m.group(1) if m else href
            results.append(FssArticle(
                article_id=article_id,
                title=title,
                published_at=published,
                category=None,
                summary=None,
                body_html=None,
                attachment_urls=[],
                source_url=self.base_url + href if href.startswith("/") else href,
            ))
        return results

    def fetch_article_body(self, article_id: str, bbs_id: str = "BBS_0001",
                            cms_cd: str = "0007") -> dict[str, Any]:
        """게시글 본문 + 첨부 URL 추출."""
        from bs4 import BeautifulSoup

        url = f"{self.base_url}/fss/main/view.do"
        params = {"bbsId": bbs_id, "cmsCd": cms_cd, "nttId": article_id}
        resp = self._client.get(url, params=params)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 본문 영역 — 일반적인 정부 게시판 selector
        body = soup.select_one(".board-view-cont, .view_cont, .cont_area, #contents")
        body_html = str(body) if body else None
        # 첨부 파일 링크
        attachments = []
        for a in soup.select("a[href*='download'], a[href*='atch']"):
            href = a.get("href", "")
            if href:
                full = self.base_url + href if href.startswith("/") else href
                attachments.append(full)

        return {
            "article_id": article_id,
            "body_html": body_html,
            "attachment_urls": attachments,
        }
