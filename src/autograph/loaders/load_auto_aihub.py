"""AI Hub 라벨 → auto.components(level=4) + vec.chunks + Neo4j CONTAINS_COMPONENT.

대상 데이터셋:
- 71347 자율주행 고장진단 — 모터-감속기 + 배터리 결함 분류 (IONIQ/KONA/NIRO)
- 578   부품 품질 검사 영상 (자동차) — 도어/그릴/루프사이드/.. 11종 OK/NG

라벨 JSON 의 ``metadata.car_model`` + ``category.{name,supercategory}`` 를 집계해
(차량_모델, 컴포넌트, 결함_클래스, 라벨_수) 통계로 변환. 통계는 **이벤트 (recalls/
complaints) 가 아니므로** `auto.events_*` 에 적재하지 않고 다음 3 곳에 분산:

1. `auto.components` (level=4 Module) — Motor-Reducer / Battery / 도어 / 범퍼 / ... (UPSERT)
2. `vec.chunks` — `(model × module)` 1 chunk 의 검색 가능 텍스트 요약
3. Neo4j — `(:VehicleModel)-[:CONTAINS_COMPONENT {source,confidence,validated_status,snapshot_year}]->(:Module)`

적재 규약:
- :Module 노드 MERGE key 는 ``{id: ...}`` (auto.components.component_id) — neo4j_init 제약과 일치.
- 공용 ``get_driver()`` + UNWIND $rows 배치 적재.

CLI:
    python -m autograph.loaders.load_auto_aihub --dataset 71347
    python -m autograph.loaders.load_auto_aihub --dataset 578
    python -m autograph.loaders.load_auto_aihub --dataset all
    python -m autograph.loaders.load_auto_aihub --dry-run

raw 위치:
    data/raw/auto/aihub/71347/.../TL.zip + VL.zip  (자동 압축해제 디렉토리도 수용)
    data/raw/auto/aihub/578/.../TL_*.tar + VL_*.tar
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from autonexusgraph.config import get_settings
from autonexusgraph.db.neo4j import get_driver
from autonexusgraph.db.postgres import get_connection
from autonexusgraph.ingestion._common import normalize_corp_name

from ..ontology import canonical_system_code


log = logging.getLogger(__name__)


# ── 컴포넌트 분류 ───────────────────────────────────────────────
# 71347 의 supercategory → (canonical_name, system_code, korean)
_71347_COMPONENT_MAP: dict[str, tuple[str, str, str]] = {
    "ECC":      ("Motor-Reducer", "powertrain", "모터-감속기"),
    "DEMAG":    ("Motor-Reducer", "powertrain", "모터-감속기"),
    "REDUC":    ("Motor-Reducer", "powertrain", "모터-감속기"),
    "NORMAL":   ("Motor-Reducer", "powertrain", "모터-감속기"),  # 컨텍스트 의존 — 라벨에서 보정
    "BAT_PACK": ("Battery Pack",  "battery",    "배터리 팩"),
}

# 578 의 부품명 (한글, tar 파일명 / 디렉토리명 — 스페이스/언더스코어 모두 수용)
# key 는 normalize 후 매칭 — tar 파일명은 "1.도어" 형태, 디렉토리는 "도어" 형태.
_578_COMPONENT_MAP: dict[str, tuple[str, str, str]] = {
    "도어":          ("Door",            "body",       "도어"),
    "라디에이터그릴": ("Radiator Grille", "body",       "라디에이터 그릴"),
    "루프사이드":    ("Roof Side",       "body",       "루프사이드"),
    "배선":          ("Wire Harness",    "electrical", "배선"),
    "범퍼":          ("Bumper",          "body",       "범퍼"),
    "카울커버":      ("Cowl Cover",      "body",       "카울커버"),
    "커넥터":        ("Connector",       "electrical", "커넥터"),
    "테일램프":      ("Tail Lamp",       "lighting",   "테일 램프"),
    "프레임":        ("Frame",           "chassis",    "프레임"),
    "헤드램프":      ("Head Lamp",       "lighting",   "헤드 램프"),
    "휀더":          ("Fender",          "body",       "휀더"),
}


def _norm_part(s: str) -> str:
    """578 의 부품명 정규화 — 공백/언더스코어/'NN.' prefix 제거."""
    import re
    s = re.sub(r"^\s*\d+\s*\.\s*", "", s or "")    # "1.도어" → "도어"
    s = s.replace(" ", "").replace("_", "").strip()
    return s


@dataclass
class LoadStats:
    components_inserted: int = 0
    chunks_inserted: int = 0
    chunks_updated: int = 0
    edges_merged: int = 0
    errors: list[str] = field(default_factory=list)


def _aihub_root(dataset: int) -> Path:
    return get_settings().ingest_raw_dir / "auto" / "aihub" / str(dataset)


# ── 71347 라벨 집계 ─────────────────────────────────────────────
def _iter_71347_labels(root: Path):
    """TL_extracted/ (이미 압축해제) 또는 TL.zip / VL.zip 직접 스트리밍 모두 지원."""
    # 1) 압축 해제된 디렉토리 우선.
    for sub in ("TL_extracted", "VL_extracted"):
        d = root / sub
        if d.exists():
            for p in d.rglob("*.json"):
                try:
                    yield json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
            return  # 한 곳이라도 있으면 거기서 끝.

    # 2) zip 안에서 스트리밍 (압축해제 안 했을 때).
    for zip_path in root.rglob("TL.zip"):
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                if not info.filename.endswith(".json"):
                    continue
                try:
                    with z.open(info) as f:
                        yield json.loads(f.read().decode("utf-8"))
                except Exception:  # noqa: BLE001
                    continue
    for zip_path in root.rglob("VL.zip"):
        with zipfile.ZipFile(zip_path) as z:
            for info in z.infolist():
                if not info.filename.endswith(".json"):
                    continue
                try:
                    with z.open(info) as f:
                        yield json.loads(f.read().decode("utf-8"))
                except Exception:  # noqa: BLE001
                    continue


def aggregate_71347(root: Path) -> dict[tuple[str, str], dict[str, int]]:
    """라벨 → {(car_model, supercategory): {class_name: count}}."""
    counts: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    total = 0
    for d in _iter_71347_labels(root):
        cat = d.get("category") or {}
        meta = d.get("metadata") or {}
        model = (meta.get("car_model") or "").strip().upper()
        sc = (cat.get("supercategory") or "").strip().upper()
        name = (cat.get("name") or "").strip().upper()
        if not (model and sc and name):
            continue
        counts[(model, sc)][name] += 1
        total += 1
    log.info("[71347] aggregated %d labels into %d (model,supercat) groups", total, len(counts))
    return {k: dict(v) for k, v in counts.items()}


# ── 578 라벨 집계 ───────────────────────────────────────────────
# 578 의 JSON 은 BOM 포함 + annotations[].category_id ↔ categories 의 mapping 이라
# 결함명은 디렉토리 구조 (`{부품}/{결함}/*.json`) 에서 직접 추출하는 게 훨씬 안전·빠름.
def _iter_578_paths(root: Path):
    """tar 안 JSON 파일의 (부품_원문, 결함_원문) 페어를 yield. tar 미해제 시 안에서 스트리밍."""
    # 1) 압축해제된 디렉토리 ({부품}/{결함}/*.json)
    for d in root.rglob("*"):
        if d.is_dir() and d.parent.name and "/162" not in str(d.parent):
            # heuristic — 너무 broad. 정확 매칭은 tar 안 path 로.
            pass
    # 2) tar 안 path 직접 사용 (가장 정확)
    for tar_path in root.rglob("[TV]L_*.tar"):
        try:
            with tarfile.open(tar_path) as t:
                for name in t.getnames():
                    if not name.endswith(".json"):
                        continue
                    # path = "{부품}/{결함}/file.json"
                    parts = name.split("/")
                    if len(parts) < 3:
                        continue
                    yield parts[0], parts[1]
        except tarfile.TarError as e:
            log.warning("[578] tar read failed %s: %s", tar_path, e)


def aggregate_578(root: Path) -> dict[str, dict[str, int]]:
    """{normalized_part_name: {defect_class: count}}."""
    counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    total = 0
    for part_raw, defect_raw in _iter_578_paths(root):
        part = _norm_part(part_raw)
        defect = (defect_raw or "").strip()
        if not (part and defect):
            continue
        counts[part][defect] += 1
        total += 1
    log.info("[578] aggregated %d labels across %d parts", total, len(counts))
    return {k: dict(v) for k, v in counts.items()}


# ── PG/Neo4j 적재 ───────────────────────────────────────────────
def _upsert_component(cur, *, canonical_name: str, system_code: str,
                      aliases: list[str], source: str,
                      level: int = 4) -> int:
    """auto.components UPSERT (default level=4 Module).

    system_code 는 canonical_system_code() 로 SCREAMING_SNAKE_CASE 정규화 후 저장 —
    AI-Hub raw 'powertrain' 같은 표기를 'POWERTRAIN' 로 통일해 :System 노드와 매칭.
    """
    name_norm = normalize_corp_name(canonical_name)
    sys_code  = canonical_system_code(system_code)
    cur.execute("""
        INSERT INTO auto.components
          (canonical_name, name_norm, system_code, aliases, source,
           confidence, validated_status, level, snapshot_year)
        VALUES (%s, %s, %s, %s, %s, 1.000, 'verified', %s,
                EXTRACT(YEAR FROM now())::SMALLINT)
        ON CONFLICT (canonical_name, system_code) DO UPDATE SET
          aliases = (SELECT array_agg(DISTINCT a) FROM unnest(
                       auto.components.aliases || EXCLUDED.aliases) a),
          level = COALESCE(auto.components.level, EXCLUDED.level)
        RETURNING component_id
    """, (canonical_name, name_norm, sys_code, aliases, source, level))
    return cur.fetchone()[0]


def _summary_text_71347(model: str, sc: str, korean_part: str,
                        class_counts: dict[str, int]) -> str:
    total = sum(class_counts.values())
    lines = [
        f"AI Hub 자율주행 고장진단 데이터 (71347) — {model} {korean_part} 결함 분류",
        f"총 라벨 수: {total:,}건",
        "결함 클래스별 라벨 수:",
    ]
    for cls, n in sorted(class_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  - {cls}: {n:,}")
    lines.append("출처: NIA 한국지능정보사회진흥원 AI 학습 데이터셋")
    return "\n".join(lines)


def _summary_text_578(part_name: str, class_counts: dict[str, int]) -> str:
    total = sum(class_counts.values())
    lines = [
        f"AI Hub 부품 품질 검사 영상 데이터 (578) — {part_name} 품질 분류",
        f"총 라벨 수: {total:,}건",
        "분류별 라벨 수:",
    ]
    for cls, n in sorted(class_counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  - {cls}: {n:,}")
    lines.append("출처: NIA 한국지능정보사회진흥원 AI 학습 데이터셋")
    return "\n".join(lines)


def _upsert_chunk(cur, *, uniq: str, source: str, text: str,
                  manufacturer_id: int | None, model_id: int | None,
                  variant_id: int | None, metadata: dict) -> str:
    """vec.chunks UPSERT — source + metadata.uniq 로 dedup. 'inserted' / 'updated'."""
    cur.execute("""
        SELECT id, text FROM vec.chunks
         WHERE source = %s AND metadata->>'uniq' = %s
         LIMIT 1
    """, (source, uniq))
    r = cur.fetchone()
    if r:
        cid, ex_text = r
        if ex_text != text:
            cur.execute("""
                UPDATE vec.chunks SET text=%s, token_count=%s,
                       manufacturer_id=COALESCE(manufacturer_id, %s),
                       model_id=COALESCE(model_id, %s),
                       variant_id=COALESCE(variant_id, %s),
                       embedding=NULL,
                       metadata = metadata || %s::jsonb
                 WHERE id=%s
            """, (text, max(1, len(text) // 4),
                  manufacturer_id, model_id, variant_id,
                  json.dumps(metadata, ensure_ascii=False, default=str), cid))
            return "updated"
        return "skipped"
    cur.execute("""
        INSERT INTO vec.chunks
          (corp_code, rcept_no, section, chunk_idx, text, token_count,
           metadata, source, manufacturer_id, model_id, variant_id)
        VALUES (NULL, NULL, %s, 0, %s, %s, %s::jsonb, %s, %s, %s, %s)
    """, (metadata.get("section", "auto.aihub"), text, max(1, len(text) // 4),
          json.dumps(metadata, ensure_ascii=False, default=str), source,
          manufacturer_id, model_id, variant_id))
    return "inserted"


def _resolve_model(cur, model_name: str) -> tuple[int | None, int | None, str | None]:
    """car_model 명 (예: 'IONIQ', 'KONA', 'NIRO') → (manufacturer_id, model_id, model_name).

    AI Hub 'IONIQ' 는 NHTSA vPIC 의 'Ioniq 5'/'Ioniq 6' 와 정확 일치하지 않음.
    name_norm prefix 매칭으로 best-effort. 미매칭 시 (None, None, None).
    Neo4j MERGE 시 name 으로 매칭하므로 PG 의 실제 name 도 함께 반환.
    """
    norm = normalize_corp_name(model_name)
    cur.execute("""
        SELECT mm.manufacturer_id, m.model_id, m.name
          FROM auto.master_vehicle_models m
          JOIN auto.master_manufacturers mm USING (manufacturer_id)
         WHERE m.name_norm = %s OR m.name_norm LIKE %s
         ORDER BY m.name_norm = %s DESC, length(m.name_norm) ASC
         LIMIT 1
    """, (norm, norm + "%", norm))
    r = cur.fetchone()
    return (r[0], r[1], r[2]) if r else (None, None, None)


# ── Neo4j ───────────────────────────────────────────────────────
# 모듈 노드 MERGE + (VehicleModel)-[:CONTAINS_COMPONENT]->(Module) 엣지 한 번에.
# AI-Hub 'IONIQ' 는 vPIC 'Ioniq', 'Ioniq 5', 'Ioniq 6' 다수에 매칭될 수 있어 LHS 가
# 여러 모델일 수 있다 (그래서 MERGE rel 가 모델당 1 엣지를 생성).
_MERGE_AIHUB_EDGES = """
UNWIND $rows AS r
MERGE (c:Module {id: r.component_id})
  ON CREATE SET c.name = r.name, c.system_code = r.system_code,
                c.source = r.source
  ON MATCH  SET c.name = coalesce(c.name, r.name),
                c.system_code = coalesce(c.system_code, r.system_code)
WITH c, r
OPTIONAL MATCH (m:VehicleModel)
 WHERE toLower(m.name) = toLower(r.model_name)
    OR toLower(m.name) STARTS WITH toLower(r.model_name)
WITH c, r, m WHERE m IS NOT NULL
MERGE (m)-[rel:CONTAINS_COMPONENT]->(c)
  ON CREATE SET rel.source_id = r.source,
                rel.source_type = 'aihub',
                rel.extraction_method = 'deterministic',
                rel.confidence_score = r.confidence,
                rel.validated_status = 'verified',
                rel.snapshot_year = r.snapshot_year
  ON MATCH  SET rel.confidence_score =
                  CASE WHEN r.confidence > rel.confidence_score
                       THEN r.confidence ELSE rel.confidence_score END,
                rel.snapshot_year = coalesce(rel.snapshot_year, r.snapshot_year)
RETURN count(rel) AS edges
"""


def _neo4j_merge_component_edges(rows: list[dict], *, batch: int = 200) -> int:
    """각 row = {model_name, component_id, name, system_code, source, confidence, snapshot_year}.

    공용 get_driver() + UNWIND 배치 적재. 노드는 :Module, 엣지는 :CONTAINS_COMPONENT.
    """
    if not rows:
        return 0
    driver = get_driver()
    n = 0
    with driver.session() as s:
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            result = s.run(_MERGE_AIHUB_EDGES, rows=chunk)
            # UNWIND 안에서 RETURN count(rel) 은 row 마다 한 행을 yield — 합산.
            for rec in result:
                n += int(rec["edges"] or 0)
    return n


# ── 메인 ────────────────────────────────────────────────────────
def load_71347(*, dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    root = _aihub_root(71347)
    if not root.exists():
        log.warning("[load:71347] root missing: %s", root)
        return stats

    log.info("[load:71347] aggregating labels...")
    agg = aggregate_71347(root)
    if not agg:
        log.warning("[load:71347] no labels found in %s", root)
        return stats

    # supercategory 별로 컴포넌트 묶기 (ECC/DEMAG/REDUC/NORMAL → Motor-Reducer 하나로 합침)
    # → model × component 단위로 합쳐 한 chunk 로.
    model_comp_classes: dict[tuple[str, str, str, str], dict[str, int]] = collections.defaultdict(dict)
    for (model, sc), class_counts in agg.items():
        comp_info = _71347_COMPONENT_MAP.get(sc)
        if not comp_info:
            log.warning("[71347] unknown supercategory: %s", sc)
            continue
        canonical, system_code, korean = comp_info
        key = (model, canonical, system_code, korean)
        for cls, n in class_counts.items():
            model_comp_classes[key][cls] = model_comp_classes[key].get(cls, 0) + n

    conn = get_connection()
    neo4j_rows: list[dict] = []
    with conn.cursor() as cur:
        # 1) 컴포넌트 master UPSERT
        for (model, canonical, system_code, korean), classes in model_comp_classes.items():
            cur.execute("SAVEPOINT sp_comp")
            try:
                cid = _upsert_component(cur,
                    canonical_name=canonical, system_code=system_code,
                    aliases=[korean], source="aihub_71347")
                cur.execute("RELEASE SAVEPOINT sp_comp")
                stats.components_inserted += 1
            except Exception as e:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_comp")
                stats.errors.append(f"71347 component {canonical}: {e}")
                continue

            # 2) (model, component) 별 vec.chunks 1건
            mfr_id, model_id, resolved_name = _resolve_model(cur, model)
            text = _summary_text_71347(model, system_code, korean, classes)
            uniq = f"aihub_71347::{model}::{canonical}"
            cur.execute("SAVEPOINT sp_chunk")
            try:
                op = _upsert_chunk(cur,
                    uniq=uniq, source="aihub_71347", text=text,
                    manufacturer_id=mfr_id, model_id=model_id, variant_id=None,
                    metadata={
                        "uniq": uniq, "section": "auto.component_defect",
                        "car_model": model, "component": canonical,
                        "system_code": system_code, "class_counts": classes,
                        "dataset_id": 71347,
                    })
                cur.execute("RELEASE SAVEPOINT sp_chunk")
                if op == "inserted":
                    stats.chunks_inserted += 1
                elif op == "updated":
                    stats.chunks_updated += 1
            except Exception as e:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_chunk")
                stats.errors.append(f"71347 chunk {uniq}: {e}")

            # 3) Neo4j edge 후보 — name prefix 매칭 (AI Hub 'IONIQ' → vPIC 'Ioniq', 'Ioniq 5'...).
            # system_code 는 canonical (POWERTRAIN/BATTERY/...) 로 통일.
            neo4j_rows.append({
                "model_name": model,  # 'IONIQ' / 'KONA' / 'NIRO' — Cypher 가 prefix 매칭
                "component_id": int(cid),
                "name": canonical,
                "system_code": canonical_system_code(system_code),
                "source": "aihub_71347", "confidence": 1.0,
                "snapshot_year": 2022,
            })

    if dry_run:
        conn.rollback()
        log.info("[load:71347] DRY-RUN rolled back. would write %d edges", len(neo4j_rows))
    else:
        conn.commit()
        if neo4j_rows:
            stats.edges_merged = _neo4j_merge_component_edges(neo4j_rows)

    log.info("[load:71347] components=%d chunks_ins=%d chunks_upd=%d edges=%d errors=%d",
             stats.components_inserted, stats.chunks_inserted, stats.chunks_updated,
             stats.edges_merged, len(stats.errors))
    return stats


def load_578(*, dry_run: bool = False) -> LoadStats:
    stats = LoadStats()
    root = _aihub_root(578)
    if not root.exists():
        log.warning("[load:578] root missing: %s", root)
        return stats

    log.info("[load:578] aggregating labels...")
    agg = aggregate_578(root)
    if not agg:
        log.warning("[load:578] no labels found in %s — 다운로드 미완 또는 미승인", root)
        return stats

    conn = get_connection()
    with conn.cursor() as cur:
        for part_name, classes in agg.items():
            comp_info = _578_COMPONENT_MAP.get(part_name)
            if not comp_info:
                log.warning("[578] unknown part: %r", part_name)
                continue
            canonical, system_code, korean = comp_info
            cur.execute("SAVEPOINT sp_comp")
            try:
                _ = _upsert_component(cur,
                    canonical_name=canonical, system_code=system_code,
                    aliases=[korean], source="aihub_578")
                cur.execute("RELEASE SAVEPOINT sp_comp")
                stats.components_inserted += 1
            except Exception as e:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_comp")
                stats.errors.append(f"578 component {canonical}: {e}")
                continue

            text = _summary_text_578(korean, classes)
            uniq = f"aihub_578::{canonical}"
            cur.execute("SAVEPOINT sp_chunk")
            try:
                op = _upsert_chunk(cur,
                    uniq=uniq, source="aihub_578", text=text,
                    manufacturer_id=None, model_id=None, variant_id=None,
                    metadata={
                        "uniq": uniq, "section": "auto.component_quality",
                        "component": canonical, "system_code": system_code,
                        "class_counts": classes, "dataset_id": 578,
                    })
                cur.execute("RELEASE SAVEPOINT sp_chunk")
                if op == "inserted":
                    stats.chunks_inserted += 1
                elif op == "updated":
                    stats.chunks_updated += 1
            except Exception as e:  # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT sp_chunk")
                stats.errors.append(f"578 chunk {uniq}: {e}")

    if dry_run:
        conn.rollback()
    else:
        conn.commit()

    log.info("[load:578] components=%d chunks_ins=%d chunks_upd=%d errors=%d",
             stats.components_inserted, stats.chunks_inserted, stats.chunks_updated,
             len(stats.errors))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.loaders.load_auto_aihub")
    ap.add_argument("--dataset", choices=["71347", "578", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.dataset in ("71347", "all"):
        load_71347(dry_run=args.dry_run)
    if args.dataset in ("578", "all"):
        load_578(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
