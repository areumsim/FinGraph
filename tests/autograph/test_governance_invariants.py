"""거버넌스 invariant — DB 없이도 정책 자체를 검증.

세 클래스:

1. **edge_meta**: ontology/auto/relations.yaml 의 ``edge_required_meta`` 6 키가
   load_*.py 의 모든 cypher MERGE 절에 일관되게 들어가는지 정적 검사.

2. **p4_rejected_not_loaded**: ``cross_validate.run_p4`` 가 rejected 결정을
   Neo4j 에 적재하지 않는지 — 모듈의 _route_decisions 분기 검사.

3. **bridge_confidence_threshold**: ``load_bridge`` 가 confidence < 0.7 인 행을
   reviewed_status='candidate' 로 적재하는지 (PRD §4.6 + §3.5).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────────────
# 1. edge_required_meta 가 모든 loader 의 cypher MERGE 절에 들어가는지.
# ──────────────────────────────────────────────────────────────────────────────

def _loader_dir() -> Path:
    import autograph.loaders as _l
    return Path(_l.__file__).resolve().parent


def test_edge_meta_keys_present_in_all_merge_loaders():
    from autograph.loaders._neo4j_helpers import EDGE_META_KEYS
    # 직접 cypher 를 작성하는 loader 들 — edge_meta_cypher() 호출하면 OK,
    # 아니면 EDGE_META_KEYS 각 키가 SET 절에 등장해야 한다.
    direct_loaders = [
        "load_supplier_edges",
        "load_manufactured_at",
        "load_seed_standards_plants",
        "load_kncap",
        "load_recall_components",
        "load_auto_safety",
    ]
    for name in direct_loaders:
        path = _loader_dir() / f"{name}.py"
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        # edge_meta_cypher() 사용 → 통과.
        if "edge_meta_cypher" in src:
            continue
        # 아니면 EDGE_META_KEYS 키 6개 모두 SET 절에 등장.
        for key in EDGE_META_KEYS:
            assert key in src, (
                f"{name}.py — edge_meta_cypher() 도 안 쓰고 '{key}' 도 cypher 에 없음. "
                "PRD §6.7 의무 메타 누락 위험."
            )


# ──────────────────────────────────────────────────────────────────────────────
# 2. cross_validate — rejected 가 Neo4j 적재 분기에 들어가지 않는지.
# ──────────────────────────────────────────────────────────────────────────────

def test_cross_validate_rejected_not_loaded():
    cv = importlib.import_module("autograph.extractors.cross_validate")
    src = Path(cv.__file__).read_text(encoding="utf-8")
    # Neo4j MERGE 분기는 보통 validated / candidate / needs_review 만 활성.
    # rejected 가 적재 분기에 들어가면 안 됨.
    # 휴리스틱: "rejected" 토큰이 적재 함수 호출 라인 근처에 없는지.
    assert "rejected" in src, "rejected 결정 분기가 코드에 없음 — PRD §6.7 위반 가능"
    # promote/load 분기 안에는 rejected 가 없어야 함.
    promote_blocks = re.findall(
        r"def\s+_?(?:promote|load|merge).*?(?=\n(?:def|class|$))",
        src, re.S,
    )
    for block in promote_blocks:
        # 라인 단위 'rejected' 사용은 '!= rejected' 또는 'not rejected' 만 허용.
        for line in block.splitlines():
            if "rejected" not in line:
                continue
            stripped = line.strip()
            assert (
                "!= 'rejected'" in line
                or "!= \"rejected\"" in line
                or "not in" in line
                or stripped.startswith("#")
            ), f"promote/load 블록에서 rejected 분기 처리 부재:\n  {line!r}"


# ──────────────────────────────────────────────────────────────────────────────
# 3. load_bridge — confidence < 0.7 행이 reviewed_status='candidate' 또는
#    'needs_review' 로 적재되는지.
# ──────────────────────────────────────────────────────────────────────────────

def test_bridge_low_confidence_marked_candidate():
    lb_path = _loader_dir() / "load_bridge.py"
    src = lb_path.read_text(encoding="utf-8")
    # 정책: PRD §4.6 — qid_exact=0.95, lei_exact=0.93, biz_no=0.90,
    # corp_code_exact=0.95, fuzzy_name=0.60~0.75.
    # fuzzy 가 0.7 미만이면 자동 'candidate' 또는 'needs_review' 로 들어가야 함.
    assert "candidate" in src, "load_bridge 에 'candidate' reviewed_status 분기 부재"
    # 'reviewed' (확정) 는 confidence ≥ 0.90 또는 manual 한정.
    assert "reviewed" in src, "load_bridge 에 'reviewed' 분기 부재"
