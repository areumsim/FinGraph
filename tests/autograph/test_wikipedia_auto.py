"""wikipedia_auto ingestion + build_chunks_auto.build_from_wikipedia 단위 검증.

DB / HTTP 모두 mock — 파일 시스템 IO 만 실제.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── ingestion 모듈 import smoke ─────────────────────────────
def test_wikipedia_auto_module_importable():
    import autograph.ingestion.wikipedia_auto as w
    assert hasattr(w, "ingest")
    assert hasattr(w, "_fetch_entity_pages")
    assert hasattr(w, "_title_from_qid")


# ── build_chunks_auto._infobox_to_text 단위 ─────────────────
def test_infobox_to_text_basic():
    from autograph.loaders.build_chunks_auto import _infobox_to_text
    txt = _infobox_to_text({"이름": "그랜저", "제조사": "현대자동차", "출시": "1986"})
    assert "이름: 그랜저" in txt
    assert "제조사: 현대자동차" in txt
    assert "출시: 1986" in txt


def test_infobox_to_text_empty_keys():
    from autograph.loaders.build_chunks_auto import _infobox_to_text
    assert _infobox_to_text(None) == ""
    assert _infobox_to_text({}) == ""
    # 빈 키/값은 무시.
    assert _infobox_to_text({"": "x", "k": ""}) == ""


# ── _strip_html 단위 ────────────────────────────────────────
def test_strip_html_removes_tags():
    from autograph.loaders.build_chunks_auto import _strip_html
    html = "<p>Hello <b>World</b></p><script>alert(1)</script>"
    txt = _strip_html(html)
    assert "Hello World" in txt
    assert "<" not in txt
    assert "alert" not in txt   # script 블록 제거


def test_strip_html_decodes_entities():
    from autograph.loaders.build_chunks_auto import _strip_html
    assert "AT&T" in _strip_html("AT&amp;T")
    assert "Q: A?" in _strip_html("Q:&#160;A?")


def test_strip_html_empty():
    from autograph.loaders.build_chunks_auto import _strip_html
    assert _strip_html("") == ""
    assert _strip_html(None) == ""


# ── build_from_wikipedia — 실제 파일·DB 모킹 ────────────────
def _write_wiki_raw(root: Path, lang: str, kind: str, eid: int, payload: dict):
    """data/raw/auto/wikipedia/{lang}/{kind}/{eid}.json 모방."""
    p = root / lang / kind / f"{eid}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_build_from_wikipedia_iterates_files(tmp_path, monkeypatch):
    from autograph.loaders import build_chunks_auto as B

    # raw 디렉토리 mock — _wikipedia_root 가 tmp 가리키게.
    monkeypatch.setattr(B, "_wikipedia_root", lambda: tmp_path)

    # 샘플 ko/models/42.json
    _write_wiki_raw(tmp_path, "ko", "models", 42, {
        "title": "현대 그랜저",
        "lang": "ko",
        "page_id": 1234,
        "revision_id": 999,
        "extract": "현대자동차의 준대형 세단.",
        "html": "<p>그랜저</p>",
        "infobox": {"제조사": "현대자동차", "차종": "준대형"},
        "last_modified": "2024-01-01",
        "raw_summary": {"fullurl": "https://ko.wikipedia.org/wiki/..."},
        "__entity": {"kind": "models", "id": 42,
                     "name": "Grandeur", "qid": "Q123"},
    })
    # 샘플 ko/manufacturers/7.json
    _write_wiki_raw(tmp_path, "ko", "manufacturers", 7, {
        "title": "현대자동차",
        "lang": "ko",
        "page_id": 1,
        "revision_id": 1,
        "extract": "한국의 자동차 제조사.",
        "html": "",
        "infobox": {"이름": "현대자동차"},
        "last_modified": "2024-01-01",
        "raw_summary": {},
        "__entity": {"kind": "manufacturers", "id": 7,
                     "name": "Hyundai", "qid": "Q55931"},
    })

    # _upsert_chunk + get_connection mock.
    upsert_calls: list[dict] = []

    def fake_upsert(cur, *, source, section, text, metadata,
                    manufacturer_id, model_id, variant_id):
        upsert_calls.append({
            "source": source, "section": section,
            "manufacturer_id": manufacturer_id, "model_id": model_id,
            "variant_id": variant_id,
            "metadata_uniq": metadata.get("uniq"),
            "text_head": text[:80],
        })
    monkeypatch.setattr(B, "_upsert_chunk", fake_upsert)

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    monkeypatch.setattr(B, "get_connection", lambda: conn)

    n = B.build_from_wikipedia()
    assert n == 2
    sources = {c["source"] for c in upsert_calls}
    assert sources == {"wikipedia_auto"}
    sections = {c["section"] for c in upsert_calls}
    assert sections == {"auto.wiki"}

    # models → model_id 채움, manufacturers → manufacturer_id 채움.
    by_kind = {c["metadata_uniq"]: c for c in upsert_calls}
    model_uniq = next(k for k in by_kind if "models" in k)
    mfr_uniq = next(k for k in by_kind if "manufacturers" in k)
    assert by_kind[model_uniq]["model_id"] == 42
    assert by_kind[model_uniq]["manufacturer_id"] is None
    assert by_kind[mfr_uniq]["manufacturer_id"] == 7
    assert by_kind[mfr_uniq]["model_id"] is None
    # variant 는 항상 None (wikipedia 는 variant 단위로 들어가지 않음).
    assert all(c["variant_id"] is None for c in upsert_calls)


def test_build_from_wikipedia_skips_empty(tmp_path, monkeypatch):
    """extract/infobox/html 모두 비어있으면 청크 안 만듦."""
    from autograph.loaders import build_chunks_auto as B
    monkeypatch.setattr(B, "_wikipedia_root", lambda: tmp_path)
    _write_wiki_raw(tmp_path, "ko", "models", 99, {
        "title": "", "lang": "ko",
        "page_id": None, "revision_id": None,
        "extract": "", "html": "", "infobox": None,
        "last_modified": None, "raw_summary": None,
        "__entity": {"kind": "models", "id": 99, "name": None, "qid": None},
    })

    upsert_calls: list[dict] = []
    monkeypatch.setattr(B, "_upsert_chunk",
        lambda cur, **kw: upsert_calls.append(kw))
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    monkeypatch.setattr(B, "get_connection", lambda: conn)

    n = B.build_from_wikipedia()
    assert n == 0
    assert upsert_calls == []


def test_build_from_wikipedia_no_root(tmp_path, monkeypatch):
    """raw 디렉토리 없으면 graceful — 0 반환."""
    from autograph.loaders import build_chunks_auto as B
    monkeypatch.setattr(B, "_wikipedia_root", lambda: tmp_path / "nope")
    n = B.build_from_wikipedia()
    assert n == 0


def test_build_from_wikipedia_truncates_long_html(tmp_path, monkeypatch):
    """max_html_chars 보다 긴 본문은 잘림."""
    from autograph.loaders import build_chunks_auto as B
    monkeypatch.setattr(B, "_wikipedia_root", lambda: tmp_path)
    long_text = "Sentence. " * 1000   # 약 10000 자
    _write_wiki_raw(tmp_path, "en", "models", 1, {
        "title": "Tesla Model Y", "lang": "en",
        "page_id": 1, "revision_id": 1,
        "extract": "An electric SUV.",
        "html": f"<p>{long_text}</p>",
        "infobox": None, "last_modified": None, "raw_summary": {},
        "__entity": {"kind": "models", "id": 1,
                     "name": "Model Y", "qid": "Q5066"},
    })

    captured: list[str] = []
    def fake_upsert(cur, *, source, section, text, metadata,
                    manufacturer_id, model_id, variant_id):
        captured.append(text)
    monkeypatch.setattr(B, "_upsert_chunk", fake_upsert)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = MagicMock()
    monkeypatch.setattr(B, "get_connection", lambda: conn)

    B.build_from_wikipedia(max_html_chars=200)
    assert captured, "청크 생성됨"
    # title + extract + truncated html < 1000 자
    assert len(captured[0]) < 1500
    assert "..." in captured[0]
