#!/usr/bin/env python3
"""데이터 품질 cross-source 검증.

수행:
1. ID 매핑 커버리지 — corp_code 별로 어떤 외부 ID 를 보유하는가
2. CEO 일치성 — DART HAS_CEO vs Wikidata P169 (rule-only, fuzzy)
3. 자회사 적재량 — Neo4j SUBSIDIARY_OF + RELATED_TO 가 비어있지 않은가
4. 회사 alias 중복 검사 — 같은 alias_norm 이 여러 corp_code 가리키나
5. 시점 메타데이터 — financials/news/sentiment 등에 published_at/snapshot 있는가

결과:
- ops.quality_checks 테이블에 row insert (severity info/warn/error)
- 마크다운 리포트: data/reports/quality_<YYYYMMDD>.md
- 콘솔 요약

사용:
    python scripts/validate_cross_source.py
    python scripts/validate_cross_source.py --no-write   # 리포트만, DB X
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.db.postgres import get_pool


INSERT_CHECK = """
INSERT INTO ops.quality_checks (check_name, target_id, severity, message, details)
VALUES (%s, %s, %s, %s, %s)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    pool = get_pool()
    checks: list[tuple] = []  # (check_name, target_id, severity, message, details_json)
    report_lines: list[str] = []

    def add(name, target, sev, msg, details=None):
        checks.append((name, target, sev, msg, json.dumps(details or {}, ensure_ascii=False)))

    today = date.today().isoformat()
    report_lines.append(f"# FinGraph Data Quality Report — {today}\n")

    # ── 1) ID 매핑 커버리지 ────────────────────────────────────
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM master.companies WHERE is_active=TRUE")
        total_corps = cur.fetchone()[0]
        cur.execute("""
            SELECT id_type, count(DISTINCT corp_code)
              FROM master.entity_map
             GROUP BY id_type
             ORDER BY 2 DESC
        """)
        coverage = cur.fetchall()
    report_lines.append(f"\n## 1. ID 매핑 커버리지 ({total_corps} active companies)\n")
    report_lines.append("| id_type | covered | pct |")
    report_lines.append("|---|---:|---:|")
    for it, n in coverage:
        pct = 100 * n / max(1, total_corps)
        report_lines.append(f"| `{it}` | {n} | {pct:.1f}% |")
        sev = "info" if pct >= 90 else ("warn" if pct >= 50 else "error")
        add("id_coverage", it, sev, f"{it}: {n}/{total_corps} ({pct:.1f}%)",
            {"id_type": it, "count": n, "pct": pct})

    # ── 2) CEO 일치성 (DART vs Wikidata P169) ─────────────────
    with pool.connection() as conn, conn.cursor() as cur:
        # DART CEO: master.person_executive_history 에서 role 포함 '대표이사'
        cur.execute("""
            SELECT peh.corp_code, p.canonical_name, peh.role
              FROM master.person_executive_history peh
              JOIN master.persons p ON p.internal_id = peh.internal_id
             WHERE peh.role LIKE '%대표%' OR peh.role LIKE '%CEO%'
        """)
        dart_ceos = defaultdict(set)
        for cc, nm, role in cur.fetchall():
            dart_ceos[cc].add(nm)
        # Wikidata CEO: wiki.wikidata_facts where property='P169'
        # value 는 QID 형태 → label 매칭은 별도 fetch 필요. 여기선 P169 존재 여부만 체크.
        cur.execute("""
            SELECT corp_code, count(*) FROM wiki.wikidata_facts
             WHERE property='P169'
             GROUP BY corp_code
        """)
        wd_p169 = {cc: n for cc, n in cur.fetchall()}

    overlap = set(dart_ceos) & set(wd_p169)
    report_lines.append(f"\n## 2. CEO 정보 출처 매핑\n")
    report_lines.append(f"- DART CEO 정보 보유 회사: **{len(dart_ceos)}**")
    report_lines.append(f"- Wikidata P169 보유 회사: **{len(wd_p169)}**")
    report_lines.append(f"- 양쪽 모두 보유 (cross-validation 가능): **{len(overlap)}**")
    add("ceo_dual_source", None,
        "info" if overlap else "warn",
        f"DART∩Wikidata CEO sources: {len(overlap)}",
        {"dart_count": len(dart_ceos), "wd_count": len(wd_p169), "both": len(overlap)})

    # ── 3) Neo4j 적재량 sanity check ──────────────────────────
    from autonexusgraph.db.neo4j import get_driver
    with get_driver().session() as session:
        cnt = {}
        for q, key in [
            ("MATCH (n:Company) RETURN count(n) as c", "Company"),
            ("MATCH (n:Person) RETURN count(n) as c", "Person"),
            ("MATCH ()-[r:SUBSIDIARY_OF]->() RETURN count(r) as c", "SUBSIDIARY_OF"),
            ("MATCH ()-[r:RELATED_TO]->() RETURN count(r) as c", "RELATED_TO"),
            ("MATCH ()-[r:EXECUTIVE_OF]->() RETURN count(r) as c", "EXECUTIVE_OF"),
            ("MATCH ()-[r:MAJOR_SHAREHOLDER_OF]->() RETURN count(r) as c", "MAJOR_SHAREHOLDER_OF"),
            ("MATCH ()-[r:CO_MENTIONED_WITH]-() RETURN count(r) as c", "CO_MENTIONED_WITH (양방)"),
            ("MATCH (n:NewsEvent) RETURN count(n) as c", "NewsEvent"),
        ]:
            cnt[key] = session.run(q).single()["c"]

    report_lines.append(f"\n## 3. Neo4j 그래프 적재량\n")
    for k, v in cnt.items():
        report_lines.append(f"- **{k}**: {v:,}")
        sev = "info" if v > 0 else "warn"
        add("neo4j_count", k, sev, f"{k}={v}", {"label_or_rel": k, "count": v})

    # ── 4) 회사 alias 중복 검사 ──────────────────────────────
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT alias_norm, count(DISTINCT corp_code) AS n
              FROM master.company_aliases
             GROUP BY alias_norm
             HAVING count(DISTINCT corp_code) > 1
             ORDER BY 2 DESC
             LIMIT 20
        """)
        dups = cur.fetchall()
    report_lines.append(f"\n## 4. 회사명 alias 충돌 ({len(dups)} norms)\n")
    if not dups:
        report_lines.append("- 충돌 없음 ✓")
    else:
        for an, n in dups[:10]:
            report_lines.append(f"- `{an}` → {n} corps")
            add("alias_conflict", an, "warn", f"alias '{an}' maps to {n} corps", {"alias_norm": an, "count": n})

    # ── 5) 시점 메타 누락 검사 ──────────────────────────────
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM news.articles WHERE published_at IS NULL")
        n_no_pub = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM news.articles")
        n_total = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM master.person_executive_history WHERE since_date IS NULL")
        n_no_since = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM master.person_executive_history")
        n_pe_total = cur.fetchone()[0]
    report_lines.append(f"\n## 5. 시점 메타 완전성\n")
    report_lines.append(f"- 뉴스 기사 — published_at NULL: {n_no_pub} / {n_total}")
    report_lines.append(f"- 임원 이력 — since_date NULL : {n_no_since} / {n_pe_total} (DART API 미제공 — 후속 보강 대상)")
    add("ts_meta_news", None, "info" if n_no_pub == 0 else "warn",
        f"news.published_at NULL: {n_no_pub}", {"null": n_no_pub, "total": n_total})

    # ── 6) Cross-validation: Wikidata QID ↔ DART corp_code ↔ Wikipedia title ──
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT
              count(*) FILTER (WHERE em_qid.id_value IS NOT NULL) AS with_qid,
              count(*) FILTER (WHERE em_wp.id_value  IS NOT NULL) AS with_wp,
              count(*) FILTER (WHERE em_qid.id_value IS NOT NULL AND em_wp.id_value IS NOT NULL) AS both
            FROM master.companies c
            LEFT JOIN master.entity_map em_qid
                   ON em_qid.corp_code = c.corp_code AND em_qid.id_type = 'wikidata_qid'
            LEFT JOIN master.entity_map em_wp
                   ON em_wp.corp_code  = c.corp_code AND em_wp.id_type  = 'wikipedia_title'
            WHERE c.is_active = TRUE
        """)
        wqid, wwp, both = cur.fetchone()
    report_lines.append(f"\n## 6. Wikidata × Wikipedia 교차 보유\n")
    report_lines.append(f"- Wikidata QID 보유: **{wqid}**")
    report_lines.append(f"- Wikipedia 제목 보유: **{wwp}**")
    report_lines.append(f"- 둘 다 보유 (cross-source 가능): **{both}**")
    add("wd_wp_cross", None, "info", f"WD∩WP: {both}", {"qid": wqid, "wp": wwp, "both": both})

    # ── 결과 저장 ────────────────────────────────────────────
    if not args.no_write:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(INSERT_CHECK, checks)

    report_path = Path("data/reports") / f"quality_{today}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\n리포트: {report_path}")
    print("\n----- 요약 -----")
    print("\n".join(report_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
