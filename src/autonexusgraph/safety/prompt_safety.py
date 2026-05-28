"""프롬프트 인젝션 1차 방어 (흡수: _legacy/v1/src/agent/prompt_safety.py).

PRD §7.5.11 — 의심 패턴 탐지 + XML 경계 escape.

방어 전략:
    1. 사용자 입력은 XML 경계 태그(`<user_question>...</user_question>`)로 감싸 LLM 에
       데이터 영역임을 명시한다 (synthesizer prompt 에서).
    2. 본문에 `</user_question>` 가 들어오면 태그 위조로 탈출 가능 → `escape_for_xml_tag`
       가 `<`/`>`/`</tag>` 패턴을 안전한 대체 문자로 치환.
    3. `## system:` / "이전 지시 무시" 같은 메타 헤더 위장 — 신호 감지해 telemetry 에 기록.

엄격한 삭제보다 **escape + 경고 + 신호 표면화** 정책이다. 실제 차단 여부는
호출부(에이전트 nodes)가 결정.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# 닫힘 태그 — "</tag>" 형태는 치환 (경계 혼란 방지)
_TAG_CLOSE_RE = re.compile(r"</\s*([A-Za-z_][A-Za-z0-9_-]*)\s*>")

# 프롬프트 탈취 시도에서 자주 나타나는 메타 문구 — 카운트해서 경고 로그
_INJECTION_PATTERNS: tuple[str, ...] = (
    r"이전\s*지시.*?무시",
    r"앞의\s*지시.*?무시",
    r"ignore\s+previous\s+(?:instructions|prompt)",
    r"disregard\s+(?:all|previous)",
    r"###\s*system",
    r"##\s*instructions?\s*##",
    r"<\s*\|\s*im_start\s*\|\s*>",
    r"<\s*\|\s*im_end\s*\|\s*>",
    r"너는\s*이제",
    r"you\s+are\s+now",
    r"\bjailbreak\b",
    r"system\s*prompt",
    r"reveal\s+your\s+prompt",
)
_INJECTION_SIGNAL_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def escape_for_xml_tag(text: str) -> str:
    """XML 경계 태그 안에 들어가는 값에서 닫힘 태그·제어문자를 무력화.

    * `</foo>` → `<\\/foo>`
    * null byte / control char 제거 (탭·개행·캐리지리턴은 유지)
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = "".join(ch for ch in text if ch in ("\t", "\n", "\r") or 0x20 <= ord(ch) < 0x10000)
    text = _TAG_CLOSE_RE.sub(r"<\\/\1>", text)
    return text


def detect_injection_signals(text: str) -> list[str]:
    """프롬프트 인젝션 의심 신호 반환. 빈 리스트면 clean."""
    if not isinstance(text, str) or not text:
        return []
    return [m.group(0) for m in _INJECTION_SIGNAL_RE.finditer(text)]


def sanitize_user_input(text: str, *, context: str = "user_input") -> tuple[str, list[str]]:
    """사용자 입력 공통 전처리. (escape 된 텍스트, 감지된 신호 목록) 반환.

    신호가 탐지되면 경고 로그만 남기고 통과시킨다 (방어는 시스템 프롬프트의
    "태그 내부는 데이터" 규칙이 담당).
    """
    signals = detect_injection_signals(text)
    if signals:
        logger.warning(
            "prompt-injection signals (%s): %s",
            context, sorted({s.lower()[:40] for s in signals}),
        )
    return escape_for_xml_tag(text), signals


__all__ = [
    "escape_for_xml_tag",
    "detect_injection_signals",
    "sanitize_user_input",
]
