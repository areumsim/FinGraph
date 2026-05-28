"""Wikidata P176 part-supplies SPARQL + loader 단위 검증."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── SPARQL_PART_SUPPLIES — 등록 + 키 정상 ──────────────────
def test_sparql_part_supplies_registered():
    from autograph.ingestion.wikidata_auto import QUERIES, SPARQL_PART_SUPPLIES

    assert "part_supplies" in QUERIES
    assert QUERIES["part_supplies"] is SPARQL_PART_SUPPLIES
    # P176 (manufactured by) 가 본문에 등장.
    assert "wdt:P176" in SPARQL_PART_SUPPLIES
    # VALUES 블록에 자동차 부품 클래스 들어 있는지.
    assert "Q1183344" in SPARQL_PART_SUPPLIES   # vehicle part
    # ko/en 라벨 서비스.
    assert 'wikibase:language "ko,en"' in SPARQL_PART_SUPPLIES


# ── loader 모듈 import smoke ────────────────────────────────
def test_loader_importable():
    from autograph.loaders import load_wikidata_part_supplies as L
    assert callable(L.load_part_supplies)
    assert hasattr(L, "LoadStats")
    assert L._WIKIDATA_PART_CONFIDENCE == 0.80
    assert L._EXTRACTOR_NAME == "wikidata_p176"


# ── load_part_supplies — 파일 IO + DB 모킹 ─────────────────
def _write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_load_part_supplies_inserts_staging(tmp_path, monkeypatch):
    """정상 row 가 INSERT 호출로 변환되는지."""
    from autograph.loaders import load_wikidata_part_supplies as L

    monkeypatch.setattr(L, "_wikidata_root", lambda: tmp_path)
    _write_jsonl(tmp_path / "part_supplies.jsonl", [
        {"part_qid": "Q44539", "partLabel": "internal combustion engine",
         "supplier_qid": "Q4504", "supplierLabel": "Bosch",
         "countryLabel": "Germany"},
        {"part_qid": "Q193039", "partLabel": "tire",
         "supplier_qid": "Q56120", "supplierLabel": "Michelin",
         "countryLabel": "France"},
    ])

    # cursor 호출 캡쳐.
    executed: list[tuple[str, tuple]] = []
    cur = MagicMock()
    def fake_execute(sql, params=None):
        executed.append((sql, params))
    def fake_fetchone():
        # ON CONFLICT 의 RETURNING (xmax=0) — 항상 True (inserted).
        return (True,)
    cur.execute = fake_execute
    cur.fetchone = fake_fetchone

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    monkeypatch.setattr(L, "get_connection", lambda: conn)

    stats = L.load_part_supplies()
    assert stats.rows_seen == 2
    assert stats.rows_inserted + stats.rows_updated == 2

    # 실제 SQL 호출 중 staging_relations INSERT 가 있어야.
    insert_calls = [(s, p) for s, p in executed if "staging_relations" in s and "INSERT" in s]
    assert len(insert_calls) == 2

    # params 의 relation_type / head_kind / tail_kind / extractor_name 확인.
    sql, params = insert_calls[0]
    assert params[0] == "SUPPLIED_BY"
    assert params[1] == "Module"      # head_kind
    assert params[3] == "Supplier"    # tail_kind
    # extractor_name / version / gate_status / confidence
    assert "wikidata_p176" in params
    assert "auto_accept" in params
    assert 0.80 in params


def test_load_part_supplies_skips_bad_rows(tmp_path, monkeypatch):
    """라벨이 QID 그대로(Wikidata label 부재) 인 row 는 skip."""
    from autograph.loaders import load_wikidata_part_supplies as L

    monkeypatch.setattr(L, "_wikidata_root", lambda: tmp_path)
    _write_jsonl(tmp_path / "part_supplies.jsonl", [
        # 좋은 row.
        {"part_qid": "Q44539", "partLabel": "engine",
         "supplier_qid": "Q4504", "supplierLabel": "Bosch"},
        # 라벨이 QID 그대로 — 라벨 누락.
        {"part_qid": "Q1", "partLabel": "Q1",
         "supplier_qid": "Q2", "supplierLabel": "Bosch"},
        # 필수 필드 누락.
        {"part_qid": "Q3", "partLabel": "x"},
    ])
    cur = MagicMock()
    cur.execute = lambda *a, **kw: None
    cur.fetchone = lambda: (True,)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    monkeypatch.setattr(L, "get_connection", lambda: conn)

    stats = L.load_part_supplies()
    assert stats.rows_seen == 3
    assert stats.rows_skipped == 2
    assert stats.rows_inserted == 1


def test_load_part_supplies_no_file(tmp_path, monkeypatch):
    """raw 파일 없으면 graceful — 빈 stats."""
    from autograph.loaders import load_wikidata_part_supplies as L
    monkeypatch.setattr(L, "_wikidata_root", lambda: tmp_path / "nope")
    stats = L.load_part_supplies()
    assert stats.rows_seen == 0
    assert stats.rows_inserted == 0
