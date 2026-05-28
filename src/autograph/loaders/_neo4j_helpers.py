"""공통 Neo4j 적재 헬퍼.

§6.7 의 의무 메타 (source_type, source_id, confidence_score, validated_status,
snapshot_year, extraction_method) 를 모든 엣지가 일관되게 가지도록 한 곳에서 강제한다.

사용:
    >>> from ._neo4j_helpers import run_batched, edge_meta_cypher
    >>> session.run(
    ...     f"MATCH (a:Module {{id:$mid}}), (b:Supplier {{entity_id:$sid}}) "
    ...     f"MERGE (a)-[r:SUPPLIED_BY]->(b) "
    ...     f"SET {edge_meta_cypher('r')}",
    ...     mid=..., sid=..., source_id=..., source_type=..., ...
    ... )

또는 UNWIND 배치 패턴:
    run_batched(session, MY_CYPHER, rows, batch=500)
"""

from __future__ import annotations

from typing import Sequence

from ..ontology import load_edge_required_meta


# 의무 메타 키 — ontology SSOT.
EDGE_META_KEYS: tuple[str, ...] = load_edge_required_meta()


def edge_meta_cypher(rel_var: str = "r") -> str:
    """모든 의무 메타를 한 줄 SET 절로.

    rows 의 각 dict 는 EDGE_META_KEYS 모두 포함해야 한다. ``snapshot_year`` 만 NULL
    fallback (적재 연도) 으로 보강 — 나머지는 누락 시 NULL 로 들어가지만 의무 메타는
    호출자가 채우는 것이 원칙.
    """
    pieces: list[str] = []
    for key in EDGE_META_KEYS:
        if key == "snapshot_year":
            pieces.append(f"{rel_var}.{key} = coalesce(r.{key}, date().year)")
        else:
            pieces.append(f"{rel_var}.{key} = r.{key}")
    return ",\n      ".join(pieces)


def run_batched(session, cypher: str, rows: Sequence[dict], batch: int = 500) -> int:
    """``session.run(cypher, rows=chunk)`` 를 ``batch`` 단위로 반복.

    rows 가 비어 있으면 0 반환. cypher 는 ``UNWIND $rows AS r`` 로 시작하는 것을 가정.
    """
    if not rows:
        return 0
    n = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        if not chunk:
            continue
        session.run(cypher, rows=chunk)
        n += len(chunk)
    return n


__all__ = ["EDGE_META_KEYS", "edge_meta_cypher", "run_batched"]
