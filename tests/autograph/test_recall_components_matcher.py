"""load_recall_components 의 토큰화 / 매칭 로직 unit test.

DB 없이 _match_one / _tokenize / _stem 만 검증.
"""

from __future__ import annotations

import pytest

from autograph.loaders.load_recall_components import _match_one, _stem, _tokenize


def _comp(cid, name, aliases=()):
    """테스트용 합성 component dict."""
    tokens = set(_tokenize(name) + _tokenize(" ".join(aliases)))
    return {
        "id": cid, "name": name,
        "name_norm": name.lower().strip(),
        "aliases": list(aliases),
        "level": 4,
        "tokens": tokens,
    }


@pytest.fixture
def catalog():
    return [
        _comp(1, "Air Bag",                 ["Airbag", "SRS", "에어백"]),
        _comp(2, "Wire Harness",            ["배선"]),
        _comp(3, "Battery Pack",            ["BMS", "배터리"]),
        _comp(4, "Automatic Transmission",  ["AT"]),
    ]


def test_exact_normalized_match(catalog):
    c, kind, conf = _match_one("Air Bag", catalog)
    assert c is not None and c["id"] == 1
    assert kind == "exact" and conf == 0.85


def test_alias_match(catalog):
    c, kind, conf = _match_one("배선", catalog)
    assert c is not None and c["id"] == 2
    assert kind == "alias" and conf == 0.80


def test_token_match_plural_form(catalog):
    """NHTSA 'AIR BAGS:FRONTAL' 같은 복수형/콜론 패턴."""
    c, kind, conf = _match_one("AIR BAGS:FRONTAL", catalog)
    assert c is not None and c["id"] == 1
    assert kind == "token" and conf == 0.65


def test_token_match_compound_text(catalog):
    """'POWER TRAIN:AUTOMATIC TRANSMISSION' → Automatic Transmission 매칭."""
    c, kind, conf = _match_one("POWER TRAIN:AUTOMATIC TRANSMISSION", catalog)
    assert c is not None and c["id"] == 4


def test_no_match_empty(catalog):
    c, kind, conf = _match_one("", catalog)
    assert c is None and kind == "" and conf == 0.0


def test_no_match_unknown(catalog):
    c, kind, conf = _match_one("This is not in the catalog at all", catalog)
    assert c is None


@pytest.mark.parametrize("raw, stem", [
    ("bags",     "bag"),       # -s
    ("airbags",  "airbag"),    # -s
    ("wirings",  "wir"),       # -ings (4글자 suffix가 먼저 매칭됨)
    ("hoses",    "hos"),       # -es
    ("ax",       "ax"),        # ≤3 → 그대로.
    ("the",      "the"),       # stop 토큰은 stem 전에 _tokenize 가 제거하지만 stem 함수만 보면 그대로.
])
def test_stem_basic(raw, stem):
    assert _stem(raw) == stem


def test_tokenize_drops_stopwords():
    out = _tokenize("THE FRONT SYSTEM OF THE CAR")
    # 'the', 'system' 제거됨 — 'front', 'of'(stop 미포함이라 남음), 'car' 남음
    assert "system" not in out
    assert "the" not in out
