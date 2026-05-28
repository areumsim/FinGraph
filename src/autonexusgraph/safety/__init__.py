"""안전·가드 계층.

흡수: _legacy/v1/src/agent/{prompt_safety, cypher_guard, language_guard}.py

PRD §7.5.11 (보안):
- 사용자 입력과 시스템 프롬프트 분리
- 검색 문서 명확한 구분자
- 의심 패턴 사전 차단
- READ-ONLY Cypher 강제

이 패키지는 LLM 호출 / Neo4j 호출 / 답변 출력 직전 wrapping 으로 사용된다.
"""

from .prompt_safety import (
    detect_injection_signals,
    escape_for_xml_tag,
    sanitize_user_input,
)
from .cypher_guard import (
    CypherGuardError,
    assert_read_only,
    assert_templates_params_match,
    extract_bind_params,
)
from .language_guard import check_korean, korean_char_ratio

__all__ = [
    "detect_injection_signals",
    "escape_for_xml_tag",
    "sanitize_user_input",
    "CypherGuardError",
    "assert_read_only",
    "assert_templates_params_match",
    "extract_bind_params",
    "check_korean",
    "korean_char_ratio",
]
