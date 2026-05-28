"""소스별 라이선스 정책 — save_raw() 호출 시 본문 저장 여부 게이트.

원칙:
- public_domain / cc0 / cc_by_*: 본문 저장 OK (출처 표기 의무는 있음)
- kogl_*: 공공누리 — 본문 저장 OK
- copyrighted: 본문 저장 금지 (메타+요약만)
- metadata_only: 약관상 메타만 (빅카인즈 등)

사용:
    from autonexusgraph.ingestion._license import allow_body, LICENSE_POLICY
    if not allow_body("news_yonhap"):
        payload.pop("body", None)
"""

from __future__ import annotations

from typing import Literal


LicenseTier = Literal[
    "public_domain",
    "cc0",
    "cc_by_4_0",
    "cc_by_sa",
    "kogl_type1",
    "kogl_type2",
    "kogl_type3",
    "kogl_type4",
    "public_partial",
    "copyrighted",
    "metadata_only",
    "unknown",
]


LICENSE_POLICY: dict[str, LicenseTier] = {
    # 공개·정부 — 본문 저장 OK
    "dart":            "public_domain",   # 전자공시 (공공)
    "fss_press":       "kogl_type1",      # 금감원 보도자료 — KOGL 1유형
    "fss_disclosure":  "kogl_type1",      # 금감원 제재정보
    "ftc":             "kogl_type1",      # 공정거래위
    "kosis":           "public_domain",   # 통계청
    "ecos":            "public_domain",   # 한국은행
    "law":             "public_domain",   # LAW.go.kr
    "kipris":          "kogl_type1",      # 특허청 — 메타·서지정보
    "sec_edgar":       "public_domain",   # SEC (미국)
    "gleif":           "cc_by_4_0",       # GLEIF LEI
    "krx":             "public_domain",   # KRX 시세 (정보데이터시스템 공개)

    # 위키 계열
    "wikipedia":       "cc_by_sa",        # 본문 OK + 출처표기
    "wikidata":        "cc0",             # 본문 OK, 무조건 자유

    # ESG (KCGS): 등급은 공개, 보고서 본문은 비공개 — 등급만 사용
    "kcgs":            "public_partial",

    # 저작권 — 메타+요약만
    "news_yonhap":     "copyrighted",
    "news_hankyung":   "copyrighted",
    "news_mois":       "kogl_type1",      # 정부 RSS — 본문 OK
    "news_moef":       "kogl_type1",      # 정부 RSS — 본문 OK
    "bigkinds":        "metadata_only",
}


BODY_ALLOWED: set[LicenseTier] = {
    "public_domain", "cc0", "cc_by_4_0", "cc_by_sa",
    "kogl_type1", "kogl_type2",
}


def allow_body(source: str) -> bool:
    """source 키의 본문 저장이 허용되는가."""
    tier = LICENSE_POLICY.get(source, "unknown")
    return tier in BODY_ALLOWED


def require_attribution(source: str) -> bool:
    """출처 표기가 필요한가 (CC BY/SA, KOGL 3·4유형, GLEIF 등)."""
    tier = LICENSE_POLICY.get(source, "unknown")
    return tier in {"cc_by_sa", "cc_by_4_0", "kogl_type3", "kogl_type4"}


def policy(source: str) -> LicenseTier:
    return LICENSE_POLICY.get(source, "unknown")
