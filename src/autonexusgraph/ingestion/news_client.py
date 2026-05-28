"""뉴스 RSS 클라이언트 — 연합뉴스 + 기타 공개 RSS.

라이선스 가드:
- ✅ RSS 피드 (제목 + 요약 + URL) 만 수집 — 표시·검색·분석 가능
- ❌ 전문 (본문) 수집·재배포 금지 (저작권). 본문 필요 시 원문 링크로
- ✅ 제목·요약은 fair use + 인용 가능

소스:
- 연합뉴스 — http://www.yonhapnewstv.co.kr/category/news/economy/feed/
  (또는 RSS 피드 URL 은 변경 가능)
- 한국경제 RSS (선택)
- 정부 보도자료 RSS (자유)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class NewsItem:
    """뉴스 항목 (RSS 표준)."""

    guid: str                  # 고유 ID
    title: str
    link: str                  # 원문 URL
    published_at: str          # ISO 8601
    summary: str | None        # description
    source: str                # feed 이름 (yonhap/hankyung/...)
    categories: list[str]


# 공개 RSS 피드 카탈로그 (검증·갱신 필요)
# - 정부/공공 RSS 는 자유
# - 민간 언론 RSS 는 제목+요약 한정 사용
KOREAN_FEEDS: dict[str, str] = {
    # 연합뉴스 — 본문 저장 X, 메타+요약만
    "yonhap_economy":  "https://www.yna.co.kr/rss/economy.xml",
    "yonhap_industry": "https://www.yna.co.kr/rss/industry.xml",
    "yonhap_market":   "https://www.yna.co.kr/rss/market.xml",
    "yonhap_finance":  "https://www.yna.co.kr/rss/finance.xml",
    # 정부 RSS — KOGL (본문 OK 가능)
    "kpf_press":       "https://www.korea.kr/rss/policy_briefing.xml",   # 대한민국 정책브리핑
    "kpf_economy":     "https://www.korea.kr/rss/policy_briefing_economy.xml",
}


class NewsRssClient:
    """RSS 피드 수집기."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.Client(timeout=timeout, headers={
            "User-Agent": "FinGraph/0.1 (research)",
        })

    def __enter__(self) -> "NewsRssClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def fetch(self, feed_url: str, source_name: str = "") -> list[NewsItem]:
        """단일 RSS 피드 → NewsItem 리스트."""
        resp = self._client.get(feed_url)
        resp.raise_for_status()
        return _parse_rss(resp.text, source_name or feed_url)

    def fetch_all(self, feeds: dict[str, str] | None = None) -> list[NewsItem]:
        feeds = feeds or KOREAN_FEEDS
        items: list[NewsItem] = []
        for name, url in feeds.items():
            try:
                items.extend(self.fetch(url, source_name=name))
            except Exception as e:
                print(f"[WARN] {name} 실패: {e}")
        return items


def _parse_rss(xml_text: str, source: str) -> list[NewsItem]:
    """RSS 2.0 또는 Atom 파싱."""
    import xml.etree.ElementTree as ET

    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # 네임스페이스 제거 (간단화)
    def strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    # RSS 2.0 — channel > item
    for item in root.iter():
        if strip_ns(item.tag) != "item":
            continue
        d = {strip_ns(c.tag): (c.text or "").strip() for c in item}
        items.append(NewsItem(
            guid=d.get("guid", d.get("link", "")),
            title=d.get("title", ""),
            link=d.get("link", ""),
            published_at=d.get("pubDate", d.get("date", "")),
            summary=d.get("description"),
            source=source,
            categories=[d[c] for c in d if c in ("category",)],
        ))
    return items
