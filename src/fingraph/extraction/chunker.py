"""문자 기반 슬라이딩 윈도우 청커.

한국어는 토크나이저 의존성이 크므로 처음엔 char 기반으로 단순화.
BGE-M3 의 토큰 한도 8K → char 약 4,000 (한국어 ~2자/토큰) 이지만
검색 품질·context 효율 위해 700~1000자 청크 권장.

_legacy/v1/src/pipeline/build_chunks.py 의 핵심 아이디어 단순화 포트:
- 길이 기준 청크 + overlap
- 문장 경계(. ! ? 또는 줄바꿈) 에서 우선 자르기
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    """단일 청크."""

    idx: int               # 섹션 내 순번 (0부터)
    text: str
    char_count: int
    token_est: int         # 대략 (char // 2)
    section_title: str | None = None


# 문장 경계 — 한국어/영어 공통
_SENT_BREAK = re.compile(r"([\.!?。](?:\s|$)|\n\n+)")


def _split_sentences(text: str) -> list[str]:
    """문장 단위로 자르기. 표 행 등은 그대로 한 단위."""
    parts: list[str] = []
    buf = []
    last = 0
    for m in _SENT_BREAK.finditer(text):
        end = m.end()
        parts.append(text[last:end])
        last = end
    if last < len(text):
        parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


def chunk_text(
    text: str,
    *,
    target_chars: int = 800,
    overlap_chars: int = 100,
    section_title: str | None = None,
) -> list[Chunk]:
    """텍스트 → 청크 리스트.

    Args:
        target_chars: 청크 목표 길이 (문자). 700~1000 권장.
        overlap_chars: 인접 청크 중복 (검색 누락 방지).
    """
    if not text or not text.strip():
        return []

    sentences = _split_sentences(text)
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    for sent in sentences:
        if buf and buf_len + len(sent) > target_chars:
            chunks.append("".join(buf).strip())
            # overlap — 뒤쪽 N자 유지하고 새 청크 시작
            if overlap_chars > 0 and buf:
                tail = "".join(buf)[-overlap_chars:]
                buf = [tail]
                buf_len = len(tail)
            else:
                buf = []
                buf_len = 0
        # 한 문장이 target 보다 길면 통째로 한 청크
        if len(sent) > target_chars and not buf:
            chunks.append(sent[:target_chars * 2])    # 안전 상한
            continue
        buf.append(sent)
        buf_len += len(sent)
    if buf:
        chunks.append("".join(buf).strip())

    return [
        Chunk(
            idx=i,
            text=c,
            char_count=len(c),
            token_est=len(c) // 2,
            section_title=section_title,
        )
        for i, c in enumerate(chunks)
        if c
    ]
