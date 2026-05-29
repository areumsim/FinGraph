"""auto loader 공용 문자열 normalize helper.

여러 loader 가 component_text / canonical_name 매칭에 동일한 normalize 규칙을
사용한다 — 정의가 한 곳에 있어야 매칭 정합성이 유지된다.

규칙: 모든 non-word 문자를 공백으로 치환 + lower + strip.
    "ELECTRICAL SYSTEM:INSTRUMENT CLUSTER/PANEL"
        → "electrical system instrument cluster panel"
"""

from __future__ import annotations

import re


_NORM = re.compile(r"[^\w]+", re.UNICODE)


def norm_text(s: str | None) -> str:
    """비-word 문자를 공백으로, lowercase + strip."""
    return _NORM.sub(" ", (s or "").lower()).strip()


__all__ = ["norm_text"]
