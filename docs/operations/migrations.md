# DB 마이그레이션 운영 가이드 (PostgreSQL)

`infra/postgres/init/*.sql` 은 docker compose 가 **빈 데이터 디렉토리** 위에서 처음 부팅할
때만 자동 적용된다. 데이터가 이미 존재하는 환경에서는 새 `.sql` 파일을 추가해도 자동으로
실행되지 않으므로 **수동 hot-apply 절차** 가 필요하다.

본 가이드는 본 PR 가 추가한 세 마이그레이션의 안전 적용 순서를 다룬다.

- `10_autograph_bom.sql` — `auto.components` 에 `level / parent_component_id / snapshot_year`
  컬럼 추가 + 기존 row backfill + 인덱스.
- `11_autograph_staging.sql` — 신규 테이블 `auto.master_suppliers`, `auto.staging_relations`.
- `12_autograph_inspections.sql` — 신규 테이블 `auto.events_inspections`
  (data.go.kr 15155857 KOTSA 수리검사내역 적재용).

> 두 마이그레이션 모두 **멱등** (`ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS`)
> 이라서 재실행해도 무해. 단 `ALTER COLUMN SET NOT NULL` / `ADD CONSTRAINT` 는 backfill 직후
> 실행되므로 순서를 지킬 것.

---

## 1. 사전 점검

```bash
# 컨테이너 상태.
make health

# 현재 적용된 init 파일 목록 확인 (다 떴는지).
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -c "\dt auto.*"
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -c "\dt bridge.*"
```

기대 결과:
- `auto.components` 테이블이 이미 존재해야 함 (07_autograph.sql 가 만들었음).
- `auto.master_suppliers` 가 **없으면** 11 번 마이그레이션이 미적용 상태.
- `auto.components` 에 `level` 컬럼이 **없으면** 10 번 마이그레이션이 미적용 상태.

```bash
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" -c "\d+ auto.components" | grep -E "level|parent_component_id|snapshot_year"
```

---

## 2. Hot-apply — 10_autograph_bom.sql

### 2.1 사전 백업 (권장)

```bash
# 컴포넌트 테이블만 가볍게 dump — 수십 row 라서 비용 없음.
pg_dump -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
        -t auto.components \
        > /tmp/auto_components_$(date +%Y%m%d_%H%M%S).sql
```

### 2.2 적용

```bash
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
     -f infra/postgres/init/10_autograph_bom.sql
```

`ALTER COLUMN level SET NOT NULL` 단계가 실패하면 backfill 이 누락된 row 가 있다는 뜻 —
직전 `UPDATE auto.components SET level = 4 WHERE level IS NULL` 가 모든 row 를 채우지
못한 케이스. 이 경우 NOT NULL 만 실패하고 컬럼 자체는 추가됨. 수동 backfill 후 재실행:

```bash
psql -d "$PG_DB" -c "UPDATE auto.components SET level = 4 WHERE level IS NULL;"
psql -d "$PG_DB" -c "ALTER TABLE auto.components ALTER COLUMN level SET NOT NULL;"
psql -d "$PG_DB" -c "ALTER TABLE auto.components
                       ADD CONSTRAINT chk_auto_comp_level CHECK (level BETWEEN 3 AND 5);"
```

### 2.3 검증

```bash
psql -d "$PG_DB" <<'SQL'
SELECT level, count(*) FROM auto.components GROUP BY level;
SELECT count(*) FROM auto.components WHERE snapshot_year IS NULL;  -- 0 이어야
\d+ auto.components
SQL
```

---

## 3. Hot-apply — 11_autograph_staging.sql

### 3.1 적용 (신규 테이블 2개 — 백업 불필요)

```bash
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
     -f infra/postgres/init/11_autograph_staging.sql
```

### 3.2 검증

```bash
psql -d "$PG_DB" <<'SQL'
\d+ auto.master_suppliers
\d+ auto.staging_relations
SELECT pg_get_indexdef(indexrelid)
  FROM pg_index
 WHERE indrelid = 'auto.staging_relations'::regclass;
SQL
```

`uq_auto_staging_merge_key` 부분 unique index 가 보여야 함. 없으면 ON CONFLICT 가 동작하지
않아 P3 staging upsert 가 중복 row 를 만들 수 있음.

### 3.3 기존 bridge.corp_entity 의 supplier 행 재정렬

`bridge.corp_entity.entity_id` 의 의미는 entity_type 무관하게 **stringified PK** (manufacturer 는
`auto.master_manufacturers.manufacturer_id`, supplier 는 `auto.master_suppliers.supplier_id`)
로 통일된다. 과거 supplier 행이 Wikidata QID 를 entity_id 로 쓴 환경이라면 다음 스크립트로
일괄 재배치:

```bash
psql -d "$PG_DB" <<'SQL'
-- 1) 기존 supplier 행을 master_suppliers 로 옮긴다 (멱등).
INSERT INTO auto.master_suppliers (name, name_norm, wikidata_qid, source, source_ref,
                                    confidence, validated_status)
SELECT be.name,
       lower(regexp_replace(coalesce(be.name, ''), '\(주\)|㈜|주식회사', '', 'g')),
       be.wikidata_qid,
       'bridge_migration',
       be.entity_id,
       coalesce(be.confidence_score, 0.80),
       CASE WHEN coalesce(be.confidence_score, 0) >= 0.95 THEN 'validated'
            ELSE 'candidate' END
  FROM bridge.corp_entity be
 WHERE be.entity_type = 'supplier'
   AND be.wikidata_qid IS NOT NULL
ON CONFLICT (wikidata_qid) DO NOTHING;

-- 2) bridge.entity_id 를 stringified supplier_id 로 갱신.
UPDATE bridge.corp_entity AS be
   SET entity_id = ms.supplier_id::text
  FROM auto.master_suppliers ms
 WHERE be.entity_type = 'supplier'
   AND be.wikidata_qid IS NOT NULL
   AND be.wikidata_qid = ms.wikidata_qid
   AND be.entity_id <> ms.supplier_id::text;

-- 3) 검증 — supplier 행의 entity_id 가 모두 정수 문자열인가.
SELECT entity_id, count(*)
  FROM bridge.corp_entity
 WHERE entity_type = 'supplier'
   AND entity_id !~ '^[0-9]+$'
 GROUP BY entity_id
 ORDER BY 1 LIMIT 5;   -- 결과가 비어야 함
SQL
```

기존 환경이 아니라 새 init 으로 띄우면 본 절차는 불필요 — `load_bridge.py` 가 처음부터
stringified supplier_id 를 쓴다.

---

## 3.5 Hot-apply — 12_autograph_inspections.sql

### 3.5.1 적용 (신규 테이블 — 백업 불필요)

```bash
psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
     -f infra/postgres/init/12_autograph_inspections.sql
```

### 3.5.2 검증

```bash
psql -d "$PG_DB" -c "\d+ auto.events_inspections"
psql -d "$PG_DB" -c "SELECT COUNT(*) FROM auto.events_inspections;"   # 0 OK (아직 적재 전)
```

### 3.5.3 데이터 적재

```bash
# CSV 다운: https://www.data.go.kr/data/15155857/fileData.do
# → data/raw/datagokr/inspections/<year>.csv
python -m autograph.ingestion.datagokr_inspections   # CSV → JSONL normalize
python -m autograph.loaders.load_datagokr_inspections # JSONL → PG UPSERT
```

raw 파일 미존재 시 두 명령 모두 graceful skip — exit 0.

---

## 4. Neo4j 제약 추가 (라벨 신설)

`neo4j_init.py` 가 `ontology/auto/entities.yaml` 의 라벨 목록을 그대로 읽어 CONSTRAINT 를
생성. 본 PR 에서 추가된 라벨: `:Module`, `:Part`, `:Supplier`, `:Complaint`, `:Standard`,
`:Plant`. 모두 `IF NOT EXISTS` 라서 멱등.

```bash
python -m autograph.loaders.neo4j_init
# 또는
make neo4j-init-auto
```

검증:

```bash
echo "SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties
       WHERE name STARTS WITH 'auto_'
       RETURN name, labelsOrTypes, properties
       ORDER BY name" | cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASSWORD"
```

기대: `auto_manufacturer_id_unique`, `auto_vehiclemodel_id_unique`,
`auto_vehiclevariant_id_unique`, `auto_system_code_unique`, `auto_module_id_unique`,
`auto_part_id_unique`, `auto_supplier_entity_id_unique`, `auto_recall_id_unique`,
`auto_complaint_id_unique`, `auto_standard_code_unique`, `auto_plant_code_unique` —
총 11 개.

---

## 5. 롤백 (필요 시)

`10_autograph_bom.sql` 는 컬럼 ADD 만 한다 — 데이터 손실 없이 컬럼 제거 가능:

```bash
psql -d "$PG_DB" <<'SQL'
ALTER TABLE auto.components DROP CONSTRAINT IF EXISTS chk_auto_comp_level;
ALTER TABLE auto.components DROP COLUMN IF EXISTS level;
ALTER TABLE auto.components DROP COLUMN IF EXISTS parent_component_id;
ALTER TABLE auto.components DROP COLUMN IF EXISTS snapshot_year;
DROP INDEX IF EXISTS auto.idx_auto_comp_level;
DROP INDEX IF EXISTS auto.idx_auto_comp_parent;
DROP INDEX IF EXISTS auto.idx_auto_rec_comp;
DROP INDEX IF EXISTS auto.idx_auto_rec_snap_yr;
DROP INDEX IF EXISTS auto.idx_auto_rec_comptext_lower;
DROP INDEX IF EXISTS auto.idx_auto_cmp_components_gin;
SQL
```

`11_autograph_staging.sql` 는 신규 테이블 — `DROP TABLE`:

```bash
psql -d "$PG_DB" -c "DROP TABLE IF EXISTS auto.staging_relations;"
psql -d "$PG_DB" -c "DROP TABLE IF EXISTS auto.master_suppliers;"
# Neo4j Supplier 노드의 entity_id 가 가리키는 PK 가 사라지므로 :Supplier 노드도 정리:
echo "MATCH (s:Supplier) DETACH DELETE s" | cypher-shell
```

---

## 6. 다음 PR 을 위한 체크리스트

신규 마이그레이션을 추가할 때:

1. 파일명에 **번호 prefix** + 설명 (`12_*.sql` 등). docker compose init 적용 순서가 알파벳순.
2. 모든 DDL 을 `IF NOT EXISTS` / `IF EXISTS` 로 멱등화.
3. backfill 이 필요한 컬럼은 `UPDATE … WHERE col IS NULL` 후 `SET NOT NULL` 분리.
4. 본 가이드에 hot-apply 절차 1 절 추가.
5. 회귀 방지 — `tests/autograph/` 에 schema 가정 (예: `assert col 'level' in table`)을
   검증하는 unit test 추가하면 미적용 환경에서 즉시 실패.
