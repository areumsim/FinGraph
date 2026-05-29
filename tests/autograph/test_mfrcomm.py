"""NHTSA Manufacturer Communications (TSB) loader 단위 검증.

DB / 파일 시스템 모킹 — TAB 파싱 + variant 매칭 + 청크 적재 시그니처만.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── ingestion 모듈 — manual mode ──────────────────────────
def test_ingestion_module_importable():
    from autograph.ingestion import nhtsa_mfrcomm as I
    assert hasattr(I, "fetch_flat_tsbs")
    assert hasattr(I, "INSTRUCTIONS")
    assert "수동 다운로드" in I.INSTRUCTIONS or "manual" in I.INSTRUCTIONS.lower() \
        or "FLAT_TSBS.zip" in I.INSTRUCTIONS


def test_fetch_flat_tsbs_returns_none_when_no_file(tmp_path, monkeypatch):
    """파일 없으면 안내 출력 후 None."""
    from autograph.ingestion import nhtsa_mfrcomm as I
    monkeypatch.setattr(I, "_raw_root", lambda: tmp_path)
    assert I.fetch_flat_tsbs() is None


def test_fetch_flat_tsbs_returns_cached_path(tmp_path, monkeypatch):
    """기존 zip 이 있으면 그대로 반환."""
    from autograph.ingestion import nhtsa_mfrcomm as I
    monkeypatch.setattr(I, "_raw_root", lambda: tmp_path)
    zp = tmp_path / "FLAT_TSBS.zip"
    zp.write_bytes(b"dummy")
    assert I.fetch_flat_tsbs() == zp


# ── loader 모듈 ────────────────────────────────────────────
def test_loader_module_importable():
    from autograph.loaders import load_auto_mfrcomm as L
    assert L._SOURCE_KEY == "nhtsa_tsb"
    assert L._SECTION == "auto.mfrcomm"
    assert len(L._COLUMNS) == 14
    assert L._COLUMNS[0] == "NHTSA_ID_NUMBER"
    assert L._COLUMNS[-1] == "SUMMARY"


def test_parse_year_handles_9999():
    from autograph.loaders.load_auto_mfrcomm import _parse_year
    assert _parse_year("2024") == 2024
    assert _parse_year("9999") is None     # sentinel
    assert _parse_year("") is None
    assert _parse_year("abc") is None
    assert _parse_year(None) is None


# ── _compose_text ──────────────────────────────────────────
def test_compose_text_full():
    from autograph.loaders.load_auto_mfrcomm import _compose_text
    txt = _compose_text({
        "COMMUNICATION_TYPE":        "Service Bulletin",
        "NHTSA_COMPONENTS":          "AIR BAGS, SEAT BELTS",
        "MFR_COMPONENT_SYSTEM":      "POWERTRAIN",
        "MFR_COMPONENT_SUBSYSTEM":   "Engine Control Module",
        "SUMMARY":                   "Reprogram ECM for stalling under load.",
    })
    assert "Service Bulletin" in txt
    assert "AIR BAGS" in txt
    assert "POWERTRAIN" in txt
    assert "Engine Control Module" in txt
    assert "Reprogram ECM" in txt


def test_compose_text_minimal():
    from autograph.loaders.load_auto_mfrcomm import _compose_text
    txt = _compose_text({"SUMMARY": "Just summary."})
    assert txt == "요약: Just summary."


def test_compose_text_empty():
    from autograph.loaders.load_auto_mfrcomm import _compose_text
    assert _compose_text({}) == ""
    assert _compose_text({"COMMUNICATION_TYPE": "  ", "SUMMARY": ""}) == ""


# ── _iter_rows — zip + TAB ─────────────────────────────────
def _make_tsb_zip(zip_path: Path, rows: list[list[str]],
                  *, inner_name: str = "FLAT_TSBS.txt") -> None:
    txt = "\n".join("\t".join(r) for r in rows)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(inner_name, txt)


def test_iter_rows_basic(tmp_path):
    from autograph.loaders.load_auto_mfrcomm import _iter_rows
    zp = tmp_path / "FLAT_TSBS.zip"
    _make_tsb_zip(zp, [
        # 14 컬럼 row.
        ["1001", "", "20240301", "TSB-23-001",
         "20240201", "INTERNAL-X",
         "Service Bulletin",
         "TESLA", "MODEL Y", "2023",
         "AIR BAGS", "POWERTRAIN", "Engine Control",
         "Reprogram for stalling."],
        # 짧은 row — 부족분 빈문자열.
        ["1002", "", "20240401", "TSB-23-002"],
    ])
    rows = list(_iter_rows(zp))
    assert len(rows) == 2
    assert rows[0]["NHTSA_ID_NUMBER"] == "1001"
    assert rows[0]["COMMUNICATION_TYPE"] == "Service Bulletin"
    assert rows[0]["SUMMARY"].startswith("Reprogram")
    assert rows[1]["NHTSA_ID_NUMBER"] == "1002"
    assert rows[1]["SUMMARY"] == ""    # 부족분 빈문자열


def test_find_zip_priorities(tmp_path, monkeypatch):
    from autograph.loaders import load_auto_mfrcomm as L
    monkeypatch.setattr(L, "_mfrcomm_root", lambda: tmp_path)
    assert L._find_zip() is None

    # FLAT_MFRCOMM.zip 이 있으면 그것 반환 (FLAT_TSBS 없을 때).
    p1 = tmp_path / "FLAT_MFRCOMM.zip"
    p1.write_bytes(b"x")
    assert L._find_zip() == p1

    # FLAT_TSBS.zip 도 있으면 그게 우선.
    p2 = tmp_path / "FLAT_TSBS.zip"
    p2.write_bytes(b"x")
    assert L._find_zip() == p2


# ── _process_row — 매칭 + UPSERT ──────────────────────────
def test_process_row_skips_no_summary():
    from autograph.loaders.load_auto_mfrcomm import _process_row, LoadStats
    cur = MagicMock()
    cur.execute = lambda *a, **kw: None
    cur.fetchone = lambda: None

    stats = LoadStats()
    _process_row(cur, {
        "NHTSA_ID_NUMBER": "9000",
        "SUMMARY": "",   # 본문 없음
        "MAKE": "Tesla", "MODEL": "Model Y", "MODEL_YEAR": "2023",
    }, stats)
    assert stats.rows_skipped == 1
    assert stats.rows_inserted == 0


def test_process_row_skips_no_id():
    from autograph.loaders.load_auto_mfrcomm import _process_row, LoadStats
    cur = MagicMock()
    cur.execute = lambda *a, **kw: None
    cur.fetchone = lambda: None
    stats = LoadStats()
    _process_row(cur, {"NHTSA_ID_NUMBER": "", "SUMMARY": "x"}, stats)
    assert stats.rows_skipped == 1


def test_process_row_variant_matched_inserts(monkeypatch):
    """variant 1개 매칭 → 1청크 insert."""
    from autograph.loaders import load_auto_mfrcomm as L

    inserts: list[tuple] = []
    cur = MagicMock()
    state = {"q": 0}

    def fake_execute(sql, params=None):
        state["q"] += 1
        if "FROM auto.master_manufacturers" in sql:
            cur._row = (10, 5)  # (mfr_id, model_id)
        elif "SELECT variant_id" in sql and "master_vehicle_variants" in sql:
            cur._fetch = [(42,)]
        elif "SELECT id, text FROM vec.chunks" in sql:
            cur._row = None          # 기존 청크 없음
        elif "INSERT INTO vec.chunks" in sql:
            inserts.append(params)
    cur.execute = fake_execute
    cur.fetchone = lambda: cur._row
    cur.fetchall = lambda: cur._fetch

    stats = L.LoadStats()
    L._process_row(cur, {
        "NHTSA_ID_NUMBER": "9000",
        "MAKE": "Hyundai", "MODEL": "Sonata", "MODEL_YEAR": "2024",
        "COMMUNICATION_TYPE": "Service Bulletin",
        "SUMMARY": "Brake pedal soft after 1000 mi — replace booster.",
        "MFR_COMPONENT_SYSTEM": "BRAKES",
    }, stats)

    assert stats.rows_inserted == 1
    assert stats.variants_touched == 1
    assert len(inserts) == 1
    params = inserts[0]
    # vec.chunks INSERT params:
    # (section, text, token_count, metadata, source, mfr_id, model_id, variant_id)
    assert params[0] == "auto.mfrcomm"
    assert "Brake pedal soft" in params[1]
    assert params[4] == "nhtsa_tsb"
    assert params[5] == 10           # manufacturer_id
    assert params[6] == 5            # model_id
    assert params[7] == 42           # variant_id


def test_process_row_unmatched_still_chunks(monkeypatch):
    """make 매칭 0 → unmatched 카운트 + manufacturer/model/variant 모두 NULL 청크."""
    from autograph.loaders import load_auto_mfrcomm as L

    inserts: list[tuple] = []
    cur = MagicMock()

    def fake_execute(sql, params=None):
        if "FROM auto.master_manufacturers" in sql:
            cur._row = None
        elif "SELECT id, text FROM vec.chunks" in sql:
            cur._row = None
        elif "INSERT INTO vec.chunks" in sql:
            inserts.append(params)
    cur.execute = fake_execute
    cur.fetchone = lambda: cur._row
    cur.fetchall = lambda: []

    stats = L.LoadStats()
    L._process_row(cur, {
        "NHTSA_ID_NUMBER": "9001",
        "MAKE": "UnknownOEM", "MODEL": "X", "MODEL_YEAR": "2024",
        "SUMMARY": "Recall remedy installs new firmware.",
    }, stats)
    assert stats.rows_unmatched == 1
    assert stats.rows_inserted == 1
    assert inserts[0][5] is None  # manufacturer_id
    assert inserts[0][6] is None  # model_id
    assert inserts[0][7] is None  # variant_id


def test_process_row_year_9999_falls_back_to_model_level(monkeypatch):
    """MODEL_YEAR=9999 → variant 매칭 skip → model 단위 1청크만."""
    from autograph.loaders import load_auto_mfrcomm as L

    inserts: list[tuple] = []
    cur = MagicMock()

    def fake_execute(sql, params=None):
        if "FROM auto.master_manufacturers" in sql:
            cur._row = (1, 2)
        elif "SELECT variant_id" in sql and "master_vehicle_variants" in sql:
            cur._fetch = []
        elif "SELECT id, text FROM vec.chunks" in sql:
            cur._row = None
        elif "INSERT INTO vec.chunks" in sql:
            inserts.append(params)
    cur.execute = fake_execute
    cur.fetchone = lambda: cur._row
    cur.fetchall = lambda: cur._fetch

    stats = L.LoadStats()
    L._process_row(cur, {
        "NHTSA_ID_NUMBER": "9002",
        "MAKE": "Ford", "MODEL": "F-150", "MODEL_YEAR": "9999",
        "SUMMARY": "Some campaign affecting all years.",
    }, stats)
    # year=None → variant_ids 빈 리스트 → model 단위 1청크.
    assert stats.rows_inserted == 1
    assert inserts[0][5] == 1
    assert inserts[0][6] == 2
    assert inserts[0][7] is None     # variant_id


# ── retrieve AUTO_SOURCES 통합 ────────────────────────────
def test_auto_sources_includes_nhtsa_tsb():
    from autograph.tools.retrieve import AUTO_SOURCES
    assert "nhtsa_tsb" in AUTO_SOURCES
