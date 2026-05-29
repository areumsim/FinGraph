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
            # PRD §10.6 정확한 정의:
            #   "Wikidata QID + LEI 매칭 confidence ≥ 0.9 비율 80%+"
            # → fuzzy name match 는 본래 candidate 라 모수 외. deterministic
            #   match (wikidata_qid / lei / business_no / corp_code / sec_cik)
            #   만 PRD 목표 측정 모수.
            # 본 메트릭은 세 모수 모두 보고:
            #   (a) all_rows       — 전체 (참고)
            #   (b) strong_match   — PRD §10.6 의 의도된 모수
            #   (c) reviewed_only  — 사람 검토 확정만
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
                }

            # (b) PRD §10.6 모수 — deterministic match 만 (fuzzy 제외).
            STRONG_METHODS = ('wikidata_qid', 'lei', 'business_no',
                              'corp_code', 'sec_cik')
            rows = _safe_query(cur, """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE confidence_score >= 0.9)
                  FROM bridge.corp_entity
                 WHERE match_method = ANY(%s)
                   AND reviewed_status <> 'rejected'
            """, (list(STRONG_METHODS),))
            if rows:
                total_s, high_s = rows[0]
                ratio_s = (float(high_s) / total_s) if total_s else 0.0
                out["bridge"]["strong_match"] = {
                    "total": int(total_s),
                    "high_confidence": int(high_s),
                    "high_confidence_ratio": round(ratio_s, 4),
                    "target_ratio": 0.80,
                    "target_met": (total_s > 0 and ratio_s >= 0.80),
                    "methods_included": list(STRONG_METHODS),
                }

            # (c) reviewed-only 모수.
            rows = _safe_query(cur, """
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE confidence_score >= 0.9)
                  FROM bridge.corp_entity
                 WHERE reviewed_status = 'reviewed'
            """)
            if rows:
                total_r, high_r = rows[0]
                ratio_r = (float(high_r) / total_r) if total_r else 0.0
                out["bridge"]["reviewed_only"] = {
                    "total": int(total_r),
                    "high_confidence": int(high_r),
                    "high_confidence_ratio": round(ratio_r, 4),
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
            # bridge.corp_entity 의 supplier row 일부가 옛 schema 잔재로
            # entity_id 에 'Q...' QID 가 들어있어 ::bigint cast 가 실패한다.
            # `entity_id ~ '^[0-9]+$'` regex 로 numeric 만 cast (안전성 보장).
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
                              AND entity_id ~ '^[0-9]+$'
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

            # bridge.corp_entity supplier 의 entity_id 패턴 진단 (장기 마이그 안내용).
            rows = _safe_query(cur, """
                SELECT
                  COUNT(*) FILTER (WHERE entity_type='supplier' AND entity_id ~ '^[0-9]+$') AS numeric_ids,
                  COUNT(*) FILTER (WHERE entity_type='supplier' AND entity_id ~ '^Q[0-9]+$') AS qid_ids,
                  COUNT(*) FILTER (WHERE entity_type='supplier'
                                    AND entity_id !~ '^[0-9]+$'
                                    AND entity_id !~ '^Q[0-9]+$') AS other_ids
                  FROM bridge.corp_entity
            """)
            if rows:
                num_ids, qid_ids, other_ids = rows[0]
                out["bridge"]["supplier_id_patterns"] = {
                    "numeric (stringified supplier_id)": int(num_ids),
                    "qid (Q\\d+ — legacy schema)":       int(qid_ids),
                    "other":                              int(other_ids),
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
    lines.append(f"- `bridge.corp_entity` 전체: **{total}** rows")
    lines.append(
        f"  - reviewed={br.get('reviewed',0)}, "
        f"candidate={br.get('candidate',0)}, "
        f"rejected={br.get('rejected',0)}"
    )
    lines.append(f"- 전체 모수 confidence ≥ 0.9: {high} ({ratio:.1%}) — 참고")

    # PRD §10.6 의 정확한 모수 — strong_match (deterministic).
    strong = br.get("strong_match") or {}
    if strong:
        s_total = strong.get("total", 0)
        s_high  = strong.get("high_confidence", 0)
        s_ratio = strong.get("high_confidence_ratio", 0.0)
        met_s   = "✅" if strong.get("target_met") else "❌"
        lines.append(
            f"- **PRD §10.6 (Wikidata QID/LEI/business_no/corp_code/sec_cik 매칭만)**: "
            f"{s_high}/{s_total} = **{s_ratio:.1%}** — 목표 80%+ {met_s}"
        )

    # reviewed-only (사람 검토 확정).
    rev = br.get("reviewed_only") or {}
    if rev:
        r_total = rev.get("total", 0)
        r_high  = rev.get("high_confidence", 0)
        r_ratio = rev.get("high_confidence_ratio", 0.0)
        lines.append(
            f"- reviewed-only (사람 확정): {r_high}/{r_total} = {r_ratio:.1%}"
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

    # supplier_id_patterns — 옛 schema 잔재 진단.
    patterns = br.get("supplier_id_patterns") or {}
    if patterns:
        qid_legacy = patterns.get("qid (Q\\d+ — legacy schema)", 0)
        numeric = patterns.get("numeric (stringified supplier_id)", 0)
        if qid_legacy:
            lines.append(
                f"- ⚠️ bridge.corp_entity supplier entity_id 패턴: "
                f"numeric={numeric}, **QID legacy={qid_legacy}** (마이그 필요), "
                f"other={patterns.get('other', 0)}"
            )
    return "\n".join(lines)


__all__ = ["collect_bridge_quality", "format_summary_md"]
