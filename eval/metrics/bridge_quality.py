"""Bridge / 데이터 품질 메트릭 (PRD §10.6).

목표:
  - QID 매칭 confidence ≥ 0.9 비율 80%+
  - manufacturer 측 wikidata_qid 보유율
  - supplier 측 reviewed_status 분포

DB 직접 쿼리 — eval runner 가 매 실행마다 manifest 에 한 번 첨부.
DB 미가용 시 모든 필드 None / 0 으로 graceful degrade.
"""

from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger(__name__)


def _safe_query(cur, sql: str, params: tuple = ()) -> list[tuple]:
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("[bridge_quality] query failed: %s", e)
        return []


def collect_bridge_quality() -> dict[str, Any]:
    """bridge.corp_entity + auto.master_* 통계.

    Returns:
        {
          "bridge": {
            "total":          전체 row,
            "reviewed":       reviewed_status='reviewed' row,
            "candidate":      reviewed_status='candidate' row,
            "rejected":       reviewed_status='rejected' row,
            "high_confidence": confidence_score >= 0.9 row,
            "high_confidence_ratio": ...,    # PRD §10.6 80%+ 목표
            "by_entity_type": {manufacturer: N, supplier: N, ...},
            "by_match_method": {wikidata_qid: N, lei: N, ...},
          },
          "manufacturers": {
            "total": ..., "with_qid": ..., "qid_coverage_ratio": ...,
          },
          "suppliers": {
            "total": ..., "with_qid": ..., "with_corp_code": ...,
          },
        }

    DB 에러 시 모든 값 0 또는 None.
    """
    out: dict[str, Any] = {
        "bridge": {},
        "manufacturers": {},
        "suppliers": {},
    }
    try:
        from autonexusgraph.db.postgres import get_connection
    except Exception as e:  # noqa: BLE001
        log.warning("[bridge_quality] PG 모듈 import 실패: %s", e)
        return out

    try:
        conn = get_connection()
    except Exception as e:  # noqa: BLE001
        log.warning("[bridge_quality] PG 연결 실패: %s", e)
        return out

    try:
        with conn.cursor() as cur:
            # bridge.corp_entity 전체 통계.
            rows = _safe_query(cur, """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE reviewed_status = 'reviewed'),
                       COUNT(*) FILTER (WHERE reviewed_status = 'candidate'),
                       COUNT(*) FILTER (WHERE reviewed_status = 'rejected'),
                       COUNT(*) FILTER (WHERE confidence_score >= 0.9)
                  FROM bridge.corp_entity
            """)
            if rows:
                total, reviewed, candidate, rejected, high_conf = rows[0]
                ratio = (float(high_conf) / total) if total else 0.0
                out["bridge"] = {
                    "total": int(total),
                    "reviewed": int(reviewed),
                    "candidate": int(candidate),
                    "rejected": int(rejected),
                    "high_confidence": int(high_conf),
                    "high_confidence_ratio": round(ratio, 4),
                    "target_ratio": 0.80,    # PRD §10.6
                    "target_met": ratio >= 0.80,
                }

            # by_entity_type 분포.
            rows = _safe_query(cur, """
                SELECT entity_type, COUNT(*)
                  FROM bridge.corp_entity
                 WHERE reviewed_status <> 'rejected'
                 GROUP BY entity_type
                 ORDER BY COUNT(*) DESC
            """)
            out["bridge"]["by_entity_type"] = {r[0]: int(r[1]) for r in rows}

            # by_match_method 분포.
            rows = _safe_query(cur, """
                SELECT match_method, COUNT(*)
                  FROM bridge.corp_entity
                 WHERE reviewed_status <> 'rejected'
                 GROUP BY match_method
                 ORDER BY COUNT(*) DESC
            """)
            out["bridge"]["by_match_method"] = {r[0]: int(r[1]) for r in rows}

            # manufacturers — QID 보유율.
            rows = _safe_query(cur, """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE wikidata_qid IS NOT NULL)
                  FROM auto.master_manufacturers
            """)
            if rows:
                total, with_qid = rows[0]
                ratio = (float(with_qid) / total) if total else 0.0
                out["manufacturers"] = {
                    "total": int(total),
                    "with_qid": int(with_qid),
                    "qid_coverage_ratio": round(ratio, 4),
                }

            # suppliers — QID/corp_code 보유율.
            rows = _safe_query(cur, """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE wikidata_qid IS NOT NULL),
                       COUNT(*) FILTER (
                         WHERE supplier_id IN (
                           SELECT entity_id::bigint
                             FROM bridge.corp_entity
                            WHERE entity_type = 'supplier'
                              AND corp_code IS NOT NULL
                              AND reviewed_status <> 'rejected'
                         )
                       )
                  FROM auto.master_suppliers
            """)
            if rows:
                total, with_qid, with_corp = rows[0]
                out["suppliers"] = {
                    "total": int(total),
                    "with_qid": int(with_qid),
                    "with_corp_code": int(with_corp),
                }
        conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("[bridge_quality] 수집 중 에러: %s", e)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    return out


def format_summary_md(quality: dict[str, Any]) -> str:
    """`summary.md` 의 bridge 섹션 — 사람이 읽기 좋은 markdown."""
    lines: list[str] = ["## Bridge 데이터 품질 (PRD §10.6)"]

    br = quality.get("bridge") or {}
    if not br:
        lines.append("- (수집 실패 또는 비어있음)")
        return "\n".join(lines)

    total = br.get("total", 0)
    high = br.get("high_confidence", 0)
    ratio = br.get("high_confidence_ratio", 0.0)
    met = "✅" if br.get("target_met") else "❌"
    lines.append(f"- `bridge.corp_entity` 전체: **{total}** rows")
    lines.append(
        f"- confidence ≥ 0.9: **{high}** "
        f"({ratio:.1%}) — 목표 80%+ {met}"
    )
    lines.append(
        f"  - reviewed={br.get('reviewed',0)}, "
        f"candidate={br.get('candidate',0)}, "
        f"rejected={br.get('rejected',0)}"
    )

    by_type = br.get("by_entity_type") or {}
    if by_type:
        lines.append("- entity_type 분포: " + ", ".join(
            f"{k}={v}" for k, v in by_type.items()
        ))
    by_meth = br.get("by_match_method") or {}
    if by_meth:
        lines.append("- match_method 분포: " + ", ".join(
            f"{k}={v}" for k, v in by_meth.items()
        ))

    mfr = quality.get("manufacturers") or {}
    if mfr:
        lines.append(
            f"- manufacturers: total={mfr.get('total',0)}, "
            f"with_qid={mfr.get('with_qid',0)} "
            f"({mfr.get('qid_coverage_ratio',0):.1%})"
        )
    sup = quality.get("suppliers") or {}
    if sup:
        lines.append(
            f"- suppliers: total={sup.get('total',0)}, "
            f"with_qid={sup.get('with_qid',0)}, "
            f"with_corp_code={sup.get('with_corp_code',0)}"
        )
    return "\n".join(lines)


__all__ = ["collect_bridge_quality", "format_summary_md"]
