"""NHTSA ODI Investigations ingestion + loader 단위 검증.

DB / Neo4j / HTTP 모두 mock — 파서·매핑·zip iteration·SQL 호출 시그니처 검증.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── ingestion 모듈 ─────────────────────────────────────────
def test_ingestion_module_importable():
    from autograph.ingestion import nhtsa_investigations as I
    assert hasattr(I, "fetch_flat_inv")
    assert I.FLAT_INV_URL.startswith("https://static.nhtsa.gov/")
    assert I.FLAT_INV_URL.endswith(".zip")
    assert "INV.txt" in I.INV_DICT_URL


# ── loader 모듈 ────────────────────────────────────────────
def test_loader_module_importable():
    from autograph.loaders import load_auto_investigations as L
    assert callable(L.load_investigations)
    assert L._SOURCE_KEY == "nhtsa_odi"
    assert L._CONFIDENCE == 0.95
    # FLAT_INV 11 컬럼.
    assert len(L._COLUMNS) == 11
    assert L._COLUMNS[0] == "NHTSA_ACTION_NUMBER"
    assert L._COLUMNS[-1] == "SUMMARY"


# ── _parse_date ────────────────────────────────────────────
def test_parse_date_valid():
    from autograph.loaders.load_auto_investigations import _parse_date
    assert _parse_date("20240315") == "2024-03-15"
    assert _parse_date("19990101") == "1999-01-01"
    assert _parse_date(" 20231231 ") == "2023-12-31"


def test_parse_date_invalid():
    from autograph.loaders.load_auto_investigations import _parse_date
    assert _parse_date(None) is None
    assert _parse_date("") is None
    assert _parse_date("2024") is None         # 짧음
    assert _parse_date("2024031") is None      # 7 자
    assert _parse_date("abcdefgh") is None
    # 00 = 진행 중인 조사의 잘못 채워진 값.
    assert _parse_date("20240000") is None
    assert _parse_date("20240015") is None     # mm=00


def test_parse_year_valid():
    from autograph.loaders.load_auto_investigations import _parse_year
    assert _parse_year("2024") == 2024
    assert _parse_year(" 2023 ") == 2023


def test_parse_year_sentinel():
    """YEAR=9999 는 '불명' 의미 → None."""
    from autograph.loaders.load_auto_investigations import _parse_year
    assert _parse_year("9999") is None
    assert _parse_year(None) is None
    assert _parse_year("") is None
    assert _parse_year("abc") is None
    # 범위 밖.
    assert _parse_year("1800") is None
    assert _parse_year("2200") is None


# ── _investigation_type ────────────────────────────────────
def test_investigation_type_known_prefixes():
    from autograph.loaders.load_auto_investigations import _investigation_type
    assert _investigation_type("PE12001") == "PE"
    assert _investigation_type("EA22002") == "EA"
    assert _investigation_type("RQ23003") == "RQ"
    assert _investigation_type("AQ20001") == "AQ"
    assert _investigation_type("DP15004") == "DP"
    # 소문자도 대응.
    assert _investigation_type("pe23005") == "PE"


def test_investigation_type_unknown():
    from autograph.loaders.load_auto_investigations import _investigation_type
    assert _investigation_type("") is None
    assert _investigation_type(None) is None
    assert _investigation_type("XX12345") is None     # 알 수 없는 prefix
    assert _investigation_type("P") is None           # 너무 짧음


# ── FLAT_INV.zip iteration ────────────────────────────────
def _make_flat_inv_zip(zip_path: Path, rows: list[list[str]],
                      *, inner_name: str = "FLAT_INV.txt") -> None:
    """TAB-delimited row 들을 zip 안 txt 로 저장."""
    txt = "\n".join("\t".join(r) for r in rows)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(inner_name, txt)


def test_iter_inv_rows_basic(tmp_path):
    from autograph.loaders.load_auto_investigations import _iter_inv_rows

    zip_path = tmp_path / "FLAT_INV.zip"
    _make_flat_inv_zip(zip_path, [
        ["PE12001", "TESLA", "MODEL Y", "2023", "AIR BAGS",
         "Tesla, Inc.", "20230315", "", "",
         "Air bag inflator may rupture",
         "NHTSA opened a preliminary evaluation..."],
        ["EA22002", "HYUNDAI", "SONATA", "2024", "ENGINE",
         "Hyundai Motor America", "20220601", "20231215", "23V456",
         "Engine stalling under load",
         "EA following PE findings — recall issued..."],
    ])

    rows = list(_iter_inv_rows(zip_path))
    assert len(rows) == 2
    assert rows[0]["NHTSA_ACTION_NUMBER"] == "PE12001"
    assert rows[0]["MAKE"] == "TESLA"
    assert rows[0]["MODEL"] == "MODEL Y"
    assert rows[0]["CDATE"] == ""
    assert rows[1]["CAMPNO"] == "23V456"
    assert rows[1]["SUMMARY"].startswith("EA following PE")


def test_iter_inv_rows_truncated_row(tmp_path):
    """뒷 컬럼이 빠진 row 도 dict 으로 — 부족분은 빈 문자열."""
    from autograph.loaders.load_auto_investigations import _iter_inv_rows
    zip_path = tmp_path / "FLAT_INV.zip"
    _make_flat_inv_zip(zip_path, [
        # 5 컬럼만 있는 끊긴 row.
        ["PE99001", "FORD", "F-150", "2020", "BRAKES"],
    ])
    rows = list(_iter_inv_rows(zip_path))
    assert len(rows) == 1
    assert rows[0]["NHTSA_ACTION_NUMBER"] == "PE99001"
    assert rows[0]["SUMMARY"] == ""
    assert rows[0]["CDATE"] == ""


def test_iter_inv_rows_no_file(tmp_path):
    from autograph.loaders.load_auto_investigations import _iter_inv_rows
    rows = list(_iter_inv_rows(tmp_path / "missing.zip"))
    assert rows == []


# ── _upsert_pg — SQL 호출 캡쳐 ────────────────────────────
def _make_cur_with_resolve(variant=None, model=None, mfr=None):
    """resolve 가 (mfr, model, variant) 반환하고 INSERT...RETURNING 도 mock."""
    cur = MagicMock()
    state = {"step": 0}

    def fake_execute(sql, params=None):
        if "FROM auto.master_manufacturers" in sql and "SELECT mm.manufacturer_id" in sql:
            cur._row = (mfr, model, variant)
        elif "INSERT INTO auto.events_investigations" in sql:
            cur._row = (12345, True)   # investigation_id, inserted=True

    def fake_fetchone():
        return cur._row

    cur.execute = fake_execute
    cur.fetchone = fake_fetchone
    return cur


def test_upsert_pg_matched_variant():
    from autograph.loaders.load_auto_investigations import (
        _upsert_pg, LoadStats,
    )
    stats = LoadStats()
    cur = _make_cur_with_resolve(variant=42, model=7, mfr=1)
    out = _upsert_pg(cur, {
        "NHTSA_ACTION_NUMBER": "PE12001",
        "MAKE": "Hyundai", "MODEL": "Sonata", "YEAR": "2024",
        "COMPNAME": "AIR BAGS", "MFR_NAME": "Hyundai Motor America",
        "ODATE": "20230315", "CDATE": "", "CAMPNO": "",
        "SUBJECT": "Air bag issue", "SUMMARY": "Long summary text...",
    }, stats)
    assert out is not None
    inserted, payload = out
    assert inserted is True
    assert payload["id"] == 12345
    assert payload["action_number"] == "PE12001"
    assert payload["investigation_type"] == "PE"
    assert payload["opened_date"] == "2023-03-15"
    assert payload["closed_date"] is None
    assert payload["variant_id"] == 42
    assert payload["model_id"] == 7
    assert payload["manufacturer_id"] == 1
    assert payload["snapshot_year"] == 2023
    assert stats.rows_unmatched == 0


def test_upsert_pg_no_action_number_skips():
    from autograph.loaders.load_auto_investigations import _upsert_pg, LoadStats
    stats = LoadStats()
    cur = _make_cur_with_resolve()
    out = _upsert_pg(cur, {"NHTSA_ACTION_NUMBER": ""}, stats)
    assert out is None


def test_upsert_pg_unmatched_counts():
    """variant + model 둘 다 매칭 안 되면 rows_unmatched 증가."""
    from autograph.loaders.load_auto_investigations import _upsert_pg, LoadStats
    stats = LoadStats()
    cur = _make_cur_with_resolve(variant=None, model=None, mfr=None)
    out = _upsert_pg(cur, {
        "NHTSA_ACTION_NUMBER": "RQ99001",
        "MAKE": "Unknown", "MODEL": "X", "YEAR": "2024",
        "ODATE": "20240101",
    }, stats)
    assert out is not None
    inserted, payload = out
    assert payload["variant_id"] is None
    assert payload["model_id"] is None
    assert stats.rows_unmatched == 1


def test_upsert_pg_year_9999_treated_as_unknown():
    """YEAR='9999' → year=None → variant 매칭 skip."""
    from autograph.loaders.load_auto_investigations import _upsert_pg, LoadStats
    stats = LoadStats()
    cur = _make_cur_with_resolve(variant=None, model=5, mfr=2)
    out = _upsert_pg(cur, {
        "NHTSA_ACTION_NUMBER": "PE45001",
        "MAKE": "Toyota", "MODEL": "Camry", "YEAR": "9999",
        "ODATE": "20240601",
    }, stats)
    assert out is not None
    _, payload = out
    # year=None → variant 매칭 안 됨 — model_id 만 있음.
    assert payload["variant_id"] is None


# ── Cypher 템플릿 등록 ────────────────────────────────────
def test_cypher_templates_registered():
    import autograph.tools  # noqa: F401 — side effect 로 TEMPLATES 병합
    from autonexusgraph.tools.cypher_templates import TEMPLATES, render_template

    for name in ("auto_investigations_by_variant",
                 "auto_investigations_by_model",
                 "auto_investigation_recall_chain"):
        assert name in TEMPLATES, f"missing template: {name}"

    cypher, bind = render_template(
        "auto_investigations_by_variant",
        {"variant_id": 1, "limit": 10},
    )
    assert "INVESTIGATED_BY" in cypher
    assert "Investigation" in cypher


# ── workers / planner 통합 ────────────────────────────────
def test_investigations_in_workers_whitelist():
    from autonexusgraph.agents.workers import _AUTO_GRAPH_ALLOWED
    assert "list_investigations_affecting" in _AUTO_GRAPH_ALLOWED
    assert "get_investigation_recall_chain" in _AUTO_GRAPH_ALLOWED


def test_planner_vehicle_recall_includes_investigations():
    """plan_auto_tasks(vehicle_recall) 가 list_investigations_affecting 도 생성."""
    from autograph.policy import plan_auto_tasks
    tasks = plan_auto_tasks(
        question="Tesla Model Y 2023 리콜",
        target_vehicles=[101],
        target_models=[55],
    )
    intents = [t["intent"] for t in tasks]
    assert "list_recalls_affecting" in intents
    assert "list_investigations_affecting" in intents
    # variant + model 각각 — 2건 이상.
    assert intents.count("list_investigations_affecting") >= 2


# ── tool 함수 import ──────────────────────────────────────
def test_tool_functions_exposed():
    from autograph.tools import (
        list_investigations_affecting,
        get_investigation_recall_chain,
    )
    assert callable(list_investigations_affecting)
    assert callable(get_investigation_recall_chain)


# ── ontology 등록 ────────────────────────────────────────
def test_ontology_has_investigation_entity():
    from autograph.ontology import load_entities, entity_key_property
    entities = load_entities()
    assert "Investigation" in entities
    assert entity_key_property("Investigation") == "id"


def test_ontology_has_investigated_by_relation():
    from autograph.ontology import load_relations, relation_endpoints
    rels = load_relations()
    assert "INVESTIGATED_BY" in rels
    from_label, to_label = relation_endpoints("INVESTIGATED_BY")
    assert from_label == "VehicleVariant"
    assert to_label == "Investigation"
