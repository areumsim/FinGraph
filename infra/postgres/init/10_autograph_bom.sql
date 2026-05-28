-- AutoGraph BOM 계층 확장 — auto.components 에 level / parent / snapshot_year 추가.
--
-- 변경 이유:
--   * Level 3 (System), Level 4 (Module), Level 5 (Part) 를 분리해서 PRD §4.4 의
--     `(VehicleModel)-[:CONTAINS_SYSTEM]->(:System)-[:CONTAINED_IN-]-(:Module)-(:Part)`
--     계층을 Neo4j 에 그대로 매핑할 수 있게 한다.
--   * §6.7 의 mandatory edge meta `snapshot_year` 가 NULL 로 새는 버그(Bug 0.5) 차단.
--
-- 멱등: 모든 ADD COLUMN 은 IF NOT EXISTS. 기존 행은 안전한 기본값으로 backfill.

SET client_encoding = 'UTF8';

-- ── 1. components: level / parent / snapshot_year ─────────────────
ALTER TABLE auto.components
  ADD COLUMN IF NOT EXISTS level                SMALLINT,
  ADD COLUMN IF NOT EXISTS parent_component_id  BIGINT REFERENCES auto.components(component_id),
  ADD COLUMN IF NOT EXISTS snapshot_year        SMALLINT;

-- backfill: 기존 행은 Module(level=4)로 기본 분류. system_taxonomy 시드가 도입한
-- 행은 별도 system_seed 로더가 level=3 으로 UPDATE 한다.
UPDATE auto.components
   SET level = 4
 WHERE level IS NULL;

ALTER TABLE auto.components
  ALTER COLUMN level SET NOT NULL,
  ALTER COLUMN level SET DEFAULT 4,
  ADD CONSTRAINT chk_auto_comp_level CHECK (level BETWEEN 3 AND 5);

-- snapshot_year 기본 = 현재 연도. (NOT NULL 강제는 데이터 정합 안정 후 별도 마이그)
UPDATE auto.components
   SET snapshot_year = EXTRACT(YEAR FROM now())::SMALLINT
 WHERE snapshot_year IS NULL;

-- 인덱스: 계층 조회 가속.
CREATE INDEX IF NOT EXISTS idx_auto_comp_level    ON auto.components(level);
CREATE INDEX IF NOT EXISTS idx_auto_comp_parent   ON auto.components(parent_component_id);


-- ── 2. events_recalls: snapshot_year 인덱스 + 매칭 메타 (component_id 는 기존) ──
CREATE INDEX IF NOT EXISTS idx_auto_rec_comp     ON auto.events_recalls(component_id);
CREATE INDEX IF NOT EXISTS idx_auto_rec_snap_yr  ON auto.events_recalls(snapshot_year);

-- component_text 토큰 매칭 보조용 부분 인덱스 (소문자 trim 후 LIKE 검색 가속).
CREATE INDEX IF NOT EXISTS idx_auto_rec_comptext_lower
  ON auto.events_recalls((lower(component_text)))
  WHERE component_text IS NOT NULL;


-- ── 3. events_complaints: components GIN — 본문 외 component 명 검색용 ──
CREATE INDEX IF NOT EXISTS idx_auto_cmp_components_gin
  ON auto.events_complaints USING GIN (components);

COMMENT ON COLUMN auto.components.level
  IS 'BOM 계층 — 3=System, 4=Module, 5=Part (PRD v2.1 §4.4).';
COMMENT ON COLUMN auto.components.parent_component_id
  IS '상위 BOM 노드. Module→System, Part→Module 트리.';
COMMENT ON COLUMN auto.components.snapshot_year
  IS '엣지 메타 §6.7 의 snapshot_year. Neo4j 적재 시 동봉.';
