"""normalize_corp_name word boundary 검증.

옛 버그: ``.replace("Co", " ")`` 가 'Connect'/'Cordova' 안 substring 까지 제거.
fix: ``\\b(Inc|Ltd|Co|Corp|...)\\b\\.?`` word boundary 매칭만.
"""

from __future__ import annotations

import pytest

from autonexusgraph.ingestion._common import normalize_corp_name


@pytest.mark.parametrize("raw, expected", [
    # ── 옛 버그 케이스 (substring 매칭으로 오염되던 것) ──
    ("Transit Connect",      "transit connect"),    # 옛: 'transit nnect'
    ("Cordova Sedan",        "cordova sedan"),      # 옛: 'rdova sedan'
    ("Bronco Sport",         "bronco sport"),       # 옛 lower 후 ok였지만 명시
    ("Mustang Mach-E",       "mustang mach-e"),
    ("Land Cruiser",         "land cruiser"),

    # ── 한글 법인격 prefix/suffix ──
    ("(주)삼성전자",          "삼성전자"),
    ("㈜삼성전자",            "삼성전자"),
    ("주식회사 삼성전자",      "삼성전자"),
    ("삼성전자(주)",          "삼성전자"),
    ("(유)현대정보기술",       "현대정보기술"),

    # ── 영문 법인격 word-boundary 매칭 ──
    ("Samsung Electronics Inc.",   "samsung electronics"),
    ("Samsung Electronics Inc",    "samsung electronics"),
    ("Tesla, Inc.",                "tesla,"),         # comma 는 보존
    ("Ford Motor Company",         "ford motor"),
    ("Hyundai Motor Co Ltd",       "hyundai motor"),
    ("Hyundai Motor Co., Ltd.",    "hyundai motor ,"),
    ("Magna International Corp.",  "magna international"),

    # ── 본질적 변화 없음 (regression 방지) ──
    ("FORD",                 "ford"),
    ("",                     ""),
    ("   ",                  ""),
])
def test_normalize_corp_name(raw, expected):
    assert normalize_corp_name(raw) == expected
