"""canspec Model 문자열 → body_class/drive_type 파서."""

from __future__ import annotations

import pytest

from autograph.loaders.load_auto_specs import parse_canspec_model_str


@pytest.mark.parametrize("raw, body, drive", [
    ("IONIQ 6 4DR SEDAN",            "Sedan",      None),
    ("PALISADE 4DR SUV AWD",         "SUV",        "AWD"),
    ("SANTA FE 4DR SUV FWD HEV",     "SUV",        "FWD"),
    ("KONA EV 4DR SUV AWD",          "SUV",        "AWD"),
    ("ELANTRA 4DR HATCHBACK FWD",    "Hatchback",  "FWD"),
    ("GENESIS G80 4DR SEDAN RWD",    "Sedan",      "RWD"),
    ("CIVIC 4DR COUPE FWD",          "Coupe",      "FWD"),
    ("F-150 PICKUP 4WD",             "Pickup",     "4WD"),
    ("Sienna VAN FWD",               "Van",        "FWD"),
    ("",                             None,         None),
    (None,                           None,         None),
    ("not a vehicle string",         None,         None),
])
def test_parse_canspec_model_str(raw, body, drive):
    if raw is None:
        out = parse_canspec_model_str("")
    else:
        out = parse_canspec_model_str(raw)
    assert out == (body, drive), f"{raw!r}: got {out}, want ({body}, {drive})"
