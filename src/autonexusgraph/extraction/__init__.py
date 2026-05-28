"""문서 파싱·청킹 모듈.

dart_parser: DART 사업보고서 zip → 섹션별 텍스트
chunker:     텍스트 → 청크 (slide window + overlap)
"""

from .chunker import Chunk, chunk_text
from .dart_parser import ParsedSection, parse_dart_zip

__all__ = ["ParsedSection", "parse_dart_zip", "Chunk", "chunk_text"]
