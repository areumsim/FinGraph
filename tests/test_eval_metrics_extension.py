"""Eval 메트릭 확장 검증 (PRD §10.6 / §10.7).

- compute_hybrid_vs_vector: hybrid vs vector multi-hop EM/F1 차이 측정
- bridge_quality.format_summary_md: dict → md 문자열 렌더
"""

from __future__ import annotations


# ── PRD §10.7 — hybrid vs vector +30%p ─────────────────────
def test_hybrid_vs_vector_target_met():
    from eval.runners.run_qa_eval import compute_hybrid_vs_vector

    summary = {
        "hybrid": {"multi_hop_n": 10, "multi_hop_em": 0.75, "multi_hop_f1": 0.80},
        "vector": {"multi_hop_n": 10, "multi_hop_em": 0.40, "multi_hop_f1": 0.45},
    }
    out = compute_hybrid_vs_vector(summary)
    assert out["available"] is True
    assert out["em_diff_pp"] == 35.0   # (0.75 - 0.40) * 100
    assert out["f1_diff_pp"] == 35.0
    assert out["target_met"] is True


def test_hybrid_vs_vector_target_unmet():
    from eval.runners.run_qa_eval import compute_hybrid_vs_vector

    summary = {
        "hybrid": {"multi_hop_n": 10, "multi_hop_em": 0.50, "multi_hop_f1": 0.55},
        "vector": {"multi_hop_n": 10, "multi_hop_em": 0.40, "multi_hop_f1": 0.45},
    }
    out = compute_hybrid_vs_vector(summary)
    assert out["available"] is True
    assert out["em_diff_pp"] == 10.0
    assert out["target_met"] is False


def test_hybrid_vs_vector_missing_adapter():
    from eval.runners.run_qa_eval import compute_hybrid_vs_vector

    summary = {
        "hybrid": {"multi_hop_n": 10, "multi_hop_em": 0.7, "multi_hop_f1": 0.8},
    }
    out = compute_hybrid_vs_vector(summary)
    assert out["available"] is False
    assert out["target_met"] is False


def test_hybrid_vs_vector_no_multi_hop_subset():
    from eval.runners.run_qa_eval import compute_hybrid_vs_vector

    # multi_hop_n 키 자체가 없음 — 골드에 multi-hop 항목이 없는 경우.
    summary = {
        "hybrid": {"n": 5, "em": 0.7, "f1": 0.8},
        "vector": {"n": 5, "em": 0.5, "f1": 0.6},
    }
    out = compute_hybrid_vs_vector(summary)
    assert out["available"] is False


# ── PRD §10.6 — bridge quality MD 렌더 ────────────────────
def test_format_summary_md_with_data():
    """PRD §10.6 의 strong_match 모수가 목표 충족 (≥80%) 시 ✅."""
    from eval.metrics.bridge_quality import format_summary_md

    quality = {
        "bridge": {
            "total": 100,
            "reviewed": 50,
            "candidate": 40,
            "rejected": 10,
            "high_confidence": 85,
            "high_confidence_ratio": 0.85,
            "by_entity_type": {"manufacturer": 60, "supplier": 30},
            "by_match_method": {"wikidata_qid": 50, "name_exact": 30, "lei": 10},
            # strong_match — PRD 모수.
            "strong_match": {
                "total": 60, "high_confidence": 55,
                "high_confidence_ratio": 0.92,
                "target_ratio": 0.80, "target_met": True,
                "methods_included": ["wikidata_qid", "lei", "business_no",
                                      "corp_code", "sec_cik"],
            },
            "reviewed_only": {
                "total": 50, "high_confidence": 48,
                "high_confidence_ratio": 0.96,
            },
        },
        "manufacturers": {
            "total": 50, "with_qid": 45, "qid_coverage_ratio": 0.90,
        },
        "suppliers": {
            "total": 30, "with_qid": 25, "with_corp_code": 8,
        },
    }
    md = format_summary_md(quality)
    assert "Bridge 데이터 품질" in md
    # mock 의 high_confidence_ratio=0.92 → format '92.0%'.
    assert "55/60" in md and "92.0%" in md   # PRD strong_match
    assert "✅" in md, "strong_match.target_met=True 면 ✅"
    assert "PRD §10.6" in md
    assert "manufacturer=60" in md
    assert "wikidata_qid=50" in md
    assert "48/50" in md and "96.0%" in md   # reviewed_only


def test_format_summary_md_target_unmet():
    """PRD §10.6 strong_match 가 80% 미달이면 ❌."""
    from eval.metrics.bridge_quality import format_summary_md

    quality = {
        "bridge": {
            "total": 100, "reviewed": 10, "candidate": 80, "rejected": 10,
            "high_confidence": 30, "high_confidence_ratio": 0.30,
            "strong_match": {
                "total": 40, "high_confidence": 12,
                "high_confidence_ratio": 0.30,
                "target_ratio": 0.80, "target_met": False,
                "methods_included": ["wikidata_qid"],
            },
        },
    }
    md = format_summary_md(quality)
    assert "❌" in md, "strong_match.target_met=False 면 ❌"
    assert "12/40" in md and "30.0%" in md


def test_format_summary_md_empty():
    from eval.metrics.bridge_quality import format_summary_md
    md = format_summary_md({"bridge": {}})
    assert "수집 실패" in md or "비어" in md


# ── DB 없이 collect_bridge_quality 호출 — graceful degrade ─
def test_collect_bridge_quality_db_unavailable(monkeypatch):
    """PG 연결 실패해도 예외 없이 빈 dict 반환."""
    from eval.metrics import bridge_quality as bq

    def fail_conn():
        raise RuntimeError("postgres down")
    monkeypatch.setattr("autonexusgraph.db.postgres.get_connection", fail_conn)

    out = bq.collect_bridge_quality()
    assert out == {"bridge": {}, "manufacturers": {}, "suppliers": {}}
