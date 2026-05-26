"""DART 사업보고서 zip 파서 — 섹션별 텍스트 추출.

DART XML 스키마 (dart4.xsd):
    <DOCUMENT>
      <DOCUMENT-NAME ACODE="11011">사업보고서</DOCUMENT-NAME>
      <COMPANY-NAME AREGCIK="00126380">삼성전자(주)</COMPANY-NAME>
      <BODY>
        <LIBRARY>
          <TITLE>회사의 개요</TITLE>
          <P>...본문...</P>
          <TABLE>...</TABLE>
        </LIBRARY>
        ...
      </BODY>
    </DOCUMENT>

ACODE 매핑:
    11011 = 사업보고서
    00760 = 감사보고서
    00761 = 연결감사보고서
    11012 = 반기보고서
    11013/11014 = 분기보고서

본 파서는 메인 보고서(ACODE=11011)만 처리. 첨부 감사보고서는 별도.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParsedSection:
    """사업보고서의 한 섹션 (TITLE + 본문 텍스트)."""

    section_idx: int           # 보고서 내 순번 (0부터)
    title: str                 # 섹션 제목 (예: "II. 사업의 내용")
    text: str                  # 정제된 본문 (태그 제거)
    char_count: int


# 텍스트 정제용 정규식
_TABLE_RE = re.compile(r"<TABLE\b.*?</TABLE>", re.DOTALL | re.IGNORECASE)
_TR_RE = re.compile(r"</?TR\b[^>]*>", re.IGNORECASE)
_TD_RE = re.compile(r"</?TD\b[^>]*>", re.IGNORECASE)
_TH_RE = re.compile(r"</?TH\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_NBSP_RE = re.compile(r"&nbsp;|\xa0")
_AMP_RE = re.compile(r"&amp;")
_LT_RE = re.compile(r"&lt;")
_GT_RE = re.compile(r"&gt;")
_QUOT_RE = re.compile(r"&quot;|&#34;|&#x22;")
_APOS_RE = re.compile(r"&apos;|&#39;|&#x27;")
_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _normalize_table(table_xml: str) -> str:
    """TABLE 을 markdown-like 행 구분으로 변환 (열은 ' | ', 행은 줄바꿈)."""
    # 행/셀 → 구분자
    s = _TR_RE.sub("\n", table_xml)
    s = _TD_RE.sub(" | ", s)
    s = _TH_RE.sub(" | ", s)
    return s


def _strip_to_text(xml: str) -> str:
    """XML → plain text. 표는 행/셀 구분 유지."""
    # 1) 표 우선 markdown-like 으로 치환
    s = _TABLE_RE.sub(lambda m: "\n" + _normalize_table(m.group(0)) + "\n", xml)
    # 2) 모든 태그 제거
    s = _TAG_RE.sub("", s)
    # 3) HTML 엔티티 디코드 (수동 — 빠름)
    s = _NBSP_RE.sub(" ", s)
    s = _AMP_RE.sub("&", s)
    s = _LT_RE.sub("<", s)
    s = _GT_RE.sub(">", s)
    s = _QUOT_RE.sub('"', s)
    s = _APOS_RE.sub("'", s)
    # 4) 공백 정리
    s = _WHITESPACE_RE.sub(" ", s)
    s = _BLANK_LINES_RE.sub("\n\n", s)
    return s.strip()


# LIBRARY 단위로 섹션 자르기 — DART 보고서가 LIBRARY 로 큰 절을 구분
_LIBRARY_RE = re.compile(
    r"<LIBRARY\b[^>]*>(.*?)</LIBRARY>", re.DOTALL | re.IGNORECASE
)
# LIBRARY 안에 TITLE 이 있으면 그게 절 제목
_TITLE_RE = re.compile(
    r"<TITLE\b[^>]*>(.*?)</TITLE>", re.DOTALL | re.IGNORECASE
)


def parse_dart_zip(zip_path: Path) -> list[ParsedSection]:
    """DART 사업보고서 zip → 섹션 리스트.

    감사보고서(ACODE=00760/00761) 파일은 skip. 메인(rcept_no.xml)만.
    """
    zip_path = Path(zip_path)
    rcept_no = zip_path.stem
    main_name = f"{rcept_no}.xml"

    with zipfile.ZipFile(zip_path) as zf:
        if main_name not in zf.namelist():
            # 비표준 — 첫 번째 underscore 없는 XML 사용
            candidates = [n for n in zf.namelist()
                          if n.endswith(".xml") and "_" not in Path(n).stem]
            if not candidates:
                return []
            main_name = candidates[0]
        xml = zf.read(main_name).decode("utf-8", errors="replace")

    return _extract_sections(xml)


def parse_dart_bytes(zip_bytes: bytes, rcept_no: str) -> list[ParsedSection]:
    """zip bytes 직접 파싱 (스트리밍 환경용)."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        main_name = f"{rcept_no}.xml"
        if main_name not in zf.namelist():
            candidates = [n for n in zf.namelist()
                          if n.endswith(".xml") and "_" not in Path(n).stem]
            if not candidates:
                return []
            main_name = candidates[0]
        xml = zf.read(main_name).decode("utf-8", errors="replace")
    return _extract_sections(xml)


def _extract_sections(xml: str) -> list[ParsedSection]:
    """XML 본문 → ParsedSection 리스트."""
    sections: list[ParsedSection] = []
    matches = _LIBRARY_RE.findall(xml)

    if not matches:
        # LIBRARY 안 잡히면 통째로 한 섹션
        text = _strip_to_text(xml)
        if text:
            sections.append(ParsedSection(
                section_idx=0, title="(full report)",
                text=text, char_count=len(text),
            ))
        return sections

    for i, lib_xml in enumerate(matches):
        # TITLE 추출 (첫 번째만)
        title_match = _TITLE_RE.search(lib_xml)
        title = _strip_to_text(title_match.group(1)) if title_match else f"section {i}"
        text = _strip_to_text(lib_xml)
        # 의미 없는 작은 섹션 skip (50자 미만은 정정 신고서 같은 잡음)
        if len(text) < 50:
            continue
        sections.append(ParsedSection(
            section_idx=i,
            title=title[:200] or f"section {i}",
            text=text,
            char_count=len(text),
        ))
    return sections
