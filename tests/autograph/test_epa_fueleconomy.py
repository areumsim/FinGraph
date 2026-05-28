"""EPA fueleconomy.gov ingestion + loader 단위 검증.

DB / HTTP 모두 mock — 파서·매핑·CSV iteration 만 실제.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── ingestion 모듈 ─────────────────────────────────────────
def test_ingestion_module_importable():
    from autograph.ingestion import epa_fueleconomy as ing
    assert hasattr(ing, "fetch_vehicles_zip")
    assert ing.EPA_VEHICLES_ZIP_URL.startswith("https://")
    assert ing.EPA_VEHICLES_ZIP_URL.endswith(".zip")


# ── loader 모듈 ────────────────────────────────────────────
def test_loader_module_importable():
    from autograph.loaders import load_auto_epa as L
    assert callable(L.load_epa)
    assert hasattr(L, "LoadStats")
    assert L._SOURCE_KEY == "epa_fueleconomy"
    assert L._CONFIDENCE == 0.95


def test_map_covers_core_fields():
    from autograph.loaders.load_auto_epa import _MAP
    csv_fields = {row[0] for row in _MAP}
    measure_keys = {row[1] for row in _MAP}

    # 핵심 연비·엔진·배출 필드 매핑 등록 확인.
    for required in ("city08", "highway08", "comb08",
                     "cylinders", "displ", "fuelType",
                     "co2", "ghgScore", "trany", "drive"):
        assert required in csv_fields, f"missing csv_field: {required}"

    # measure_key 컨벤션 — spec.* 접두사.
    for key in measure_keys:
        assert key.startswith("spec."), f"invalid measure_key: {key}"


def test_map_value_types_valid():
    from autograph.loaders.load_auto_epa import _MAP
    allowed = {"num", "score", "text", "yn", "count"}
    for csv_field, measure_key, unit, vtype in _MAP:
        assert vtype in allowed, f"{csv_field}: {vtype}"


# ── _parse_value ───────────────────────────────────────────
def test_parse_value_num():
    from autograph.loaders.load_auto_epa import _parse_value
    assert _parse_value("25", "num") == (25.0, None)
    assert _parse_value("3.5", "num") == (3.5, None)
    # sentinel -1 = missing.
    assert _parse_value("-1", "num") == (None, None)
    # 빈 문자열 = missing.
    assert _parse_value("", "num") == (None, None)
    assert _parse_value(None, "num") == (None, None)
    # 잘못된 형식.
    assert _parse_value("abc", "num") == (None, None)


def test_parse_value_score_bounds():
    """ghgScore / feScore 는 1~10 범위. 0 / 11 / -1 모두 missing."""
    from autograph.loaders.load_auto_epa import _parse_value
    assert _parse_value("7", "score") == (7.0, None)
    assert _parse_value("10", "score") == (10.0, None)
    assert _parse_value("1", "score") == (1.0, None)
    assert _parse_value("-1", "score") == (None, None)
    assert _parse_value("11", "score") == (None, None)


def test_parse_value_count():
    from autograph.loaders.load_auto_epa import _parse_value
    assert _parse_value("4", "count") == (4.0, None)
    assert _parse_value("8", "count") == (8.0, None)
    assert _parse_value("-1", "count") == (None, None)


def test_parse_value_text():
    from autograph.loaders.load_auto_epa import _parse_value
    assert _parse_value("Regular Gasoline", "text") == (None, "Regular Gasoline")
    assert _parse_value("E85", "text") == (None, "E85")
    # 무의미한 sentinel.
    assert _parse_value("N/A", "text") == (None, None)
    assert _parse_value("-", "text") == (None, None)
    assert _parse_value("", "text") == (None, None)


def test_parse_value_yn():
    """sCharger / tCharger / startStop — 'Y' 또는 's'/'t' 만 의미 있음."""
    from autograph.loaders.load_auto_epa import _parse_value
    assert _parse_value("Y", "yn") == (None, "Y")
    assert _parse_value("yes", "yn") == (None, "Y")
    assert _parse_value("S", "yn") == (None, "Y")
    assert _parse_value("T", "yn") == (None, "Y")
    # N / 빈문자 = skip.
    assert _parse_value("N", "yn") == (None, None)
    assert _parse_value("", "yn") == (None, None)


# ── CSV iteration — zip / 평문 모두 ────────────────────────
def _write_zip_csv(zip_path: Path, csv_content: str, *, name: str = "vehicles.csv"):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, csv_content)


def test_iter_csv_rows_from_zip(tmp_path):
    from autograph.loaders.load_auto_epa import _iter_csv_rows

    csv_data = (
        "id,year,make,model,city08,highway08,comb08,cylinders,displ,"
        "fuelType,co2,ghgScore,trany,drive\n"
        "1,2023,Tesla,Model Y,121,112,117,-1,-1,Electricity,0,10,Automatic,All-Wheel Drive\n"
        "2,2024,Hyundai,Sonata,28,38,32,4,2.5,Regular Gasoline,278,7,Automatic,Front-Wheel Drive\n"
    )
    zip_path = tmp_path / "vehicles.csv.zip"
    _write_zip_csv(zip_path, csv_data)

    rows = list(_iter_csv_rows(zip_path))
    assert len(rows) == 2
    assert rows[0]["make"] == "Tesla"
    assert rows[0]["model"] == "Model Y"
    assert rows[0]["year"] == "2023"
    assert rows[1]["fuelType"] == "Regular Gasoline"


def test_iter_csv_rows_from_plain_csv(tmp_path):
    from autograph.loaders.load_auto_epa import _iter_csv_rows

    csv_data = "id,year,make,model\n42,2022,Kia,EV6\n"
    p = tmp_path / "vehicles.csv"
    p.write_text(csv_data, encoding="utf-8")
    rows = list(_iter_csv_rows(p))
    assert rows == [{"id": "42", "year": "2022", "make": "Kia", "model": "EV6"}]


# ── _process_row — variant 매칭 + insert ─────────────────
def test_process_row_skips_unmatched(tmp_path, monkeypatch):
    """variant 매칭 0 → unmatched 카운트만 증가, INSERT 호출 없음."""
    from autograph.loaders import load_auto_epa as L

    inserts: list[tuple] = []
    cur = MagicMock()

    def fake_execute(sql, params=None):
        if sql.strip().startswith("SELECT v.variant_id"):
            cur._last_select = []
        elif "INSERT INTO auto.spec_measurements" in sql:
            inserts.append(params)
    cur.execute = fake_execute
    cur.fetchall = lambda: cur._last_select if hasattr(cur, "_last_select") else []

    stats = L.LoadStats()
    L._process_row(cur, {
        "id": "1", "year": "2024", "make": "FakeMake", "model": "FakeModel",
        "city08": "30", "highway08": "40",
    }, stats, year_min=None)

    assert stats.rows_unmatched == 1
    assert stats.rows_matched == 0
    assert inserts == []


def test_process_row_inserts_measurements(monkeypatch):
    """매칭된 variant 1개에 정상 측정값 다수 insert 되는지."""
    from autograph.loaders import load_auto_epa as L

    inserts: list[tuple] = []
    deletes: list[tuple] = []
    cur = MagicMock()
    cur.rowcount = 0

    def fake_execute(sql, params=None):
        if sql.strip().startswith("SELECT v.variant_id"):
            cur._fetch = [(42,)]
        elif sql.strip().startswith("DELETE FROM auto.spec_measurements"):
            deletes.append(params)
            cur.rowcount = 0
        elif "INSERT INTO auto.spec_measurements" in sql:
            inserts.append(params)

    cur.execute = fake_execute
    cur.fetchall = lambda: cur._fetch
    cur.fetchone = lambda: None

    stats = L.LoadStats()
    L._process_row(cur, {
        "id": "100",
        "year": "2024", "make": "Hyundai", "model": "Sonata",
        "city08": "28", "highway08": "38", "comb08": "32",
        "cylinders": "4", "displ": "2.5",
        "fuelType": "Regular Gasoline",
        "co2": "278",
        "ghgScore": "7",
        "trany": "Automatic",
        "drive": "Front-Wheel Drive",
        "tCharger": "T",
        "startStop": "",
        "VClass": "Midsize Cars",
    }, stats, year_min=None)

    assert stats.rows_matched == 1
    assert stats.variants_touched == 1
    # 매핑 _MAP 에서 의미 있는 값을 가진 측정값 수.
    assert stats.measurements_inserted >= 10
    # 모든 INSERT 가 variant_id=42 + source='epa_fueleconomy' + confidence=0.95.
    for p in inserts:
        assert p[0] == 42      # variant_id
        assert p[5] == "epa_fueleconomy"   # source
        assert p[7] == 0.95    # confidence


def test_process_row_year_filter(monkeypatch):
    """year_min 미만 row 는 매칭 시도 없이 filtered."""
    from autograph.loaders import load_auto_epa as L

    cur = MagicMock()
    cur.execute = lambda *a, **kw: None
    cur.fetchall = lambda: []

    stats = L.LoadStats()
    L._process_row(cur, {
        "year": "1999", "make": "Hyundai", "model": "Sonata",
        "city08": "20",
    }, stats, year_min=2020)
    assert stats.rows_year_filtered == 1
    assert stats.rows_matched == 0


def test_process_row_no_useful_measurements(monkeypatch):
    """모든 측정값이 sentinel — variant 매칭돼도 INSERT 없음."""
    from autograph.loaders import load_auto_epa as L

    inserts: list = []
    cur = MagicMock()
    cur.rowcount = 0

    def fake_execute(sql, params=None):
        if sql.strip().startswith("SELECT v.variant_id"):
            cur._fetch = [(42,)]
        elif "INSERT INTO auto.spec_measurements" in sql:
            inserts.append(params)
    cur.execute = fake_execute
    cur.fetchall = lambda: cur._fetch

    stats = L.LoadStats()
    L._process_row(cur, {
        "year": "2024", "make": "X", "model": "Y",
        "city08": "-1", "highway08": "", "comb08": "-1",
        "cylinders": "-1", "displ": "-1",
        "fuelType": "N/A", "co2": "-1",
        "ghgScore": "-1", "feScore": "-1",
    }, stats, year_min=None)
    assert stats.rows_matched == 1
    # 매칭은 됐지만 insert 측정값 0 — INSERT 없어야.
    assert inserts == []
    assert stats.measurements_inserted == 0


# ── load_epa — 파일 + DB 통합 ──────────────────────────────
def test_load_epa_no_file(tmp_path, monkeypatch):
    """raw 디렉토리 없으면 graceful — 빈 stats."""
    from autograph.loaders import load_auto_epa as L
    monkeypatch.setattr(L, "_epa_root", lambda: tmp_path / "nope")
    stats = L.load_epa()
    assert stats.rows_seen == 0


def test_load_epa_end_to_end_with_zip(tmp_path, monkeypatch):
    """raw zip 만들고 → load_epa 가 행을 다 처리 + INSERT 호출."""
    from autograph.loaders import load_auto_epa as L

    csv_data = (
        "id,year,make,model,city08,highway08,comb08,cylinders,displ,"
        "fuelType,co2,ghgScore,feScore,trany,drive,VClass\n"
        "1,2024,Hyundai,Sonata,28,38,32,4,2.5,Regular Gasoline,278,7,7,Automatic,Front-Wheel Drive,Midsize Cars\n"
        "2,2024,Tesla,Model Y,121,112,117,-1,-1,Electricity,0,10,10,Automatic,All-Wheel Drive,Small SUV\n"
    )
    monkeypatch.setattr(L, "_epa_root", lambda: tmp_path)
    _write_zip_csv(tmp_path / "vehicles.csv.zip", csv_data)

    # 매칭 mock — Hyundai Sonata 매치, Tesla Model Y 미매치.
    inserts: list = []
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    def fake_execute(sql, params=None):
        if sql.strip().startswith("SELECT v.variant_id"):
            mfr = (params[0] if params else "") or ""
            if "hyundai" in mfr.lower():
                cur._fetch = [(101,)]
            else:
                cur._fetch = []
        elif "INSERT INTO auto.spec_measurements" in sql:
            inserts.append(params)
    cur.execute = fake_execute
    cur.fetchall = lambda: cur._fetch
    cur.rowcount = 0

    monkeypatch.setattr(L, "get_connection", lambda: conn)

    stats = L.load_epa()
    assert stats.rows_seen == 2
    assert stats.rows_matched == 1     # Sonata 만
    assert stats.rows_unmatched == 1   # Model Y 미매치
    assert stats.variants_touched == 1
    assert stats.measurements_inserted >= 10
    # 모든 INSERT variant 101 + source 'epa_fueleconomy'.
    for p in inserts:
        assert p[0] == 101
        assert p[5] == "epa_fueleconomy"
