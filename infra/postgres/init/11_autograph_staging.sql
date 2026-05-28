-- AutoGraph 공급사 마스터 + P3 staging.
--
-- 1. auto.master_suppliers — Supplier 노드의 PG SSOT.
--    bridge.corp_entity 의 supplier 행은 'entity_id' 가 Wikidata QID 였는데 (Bug 0.10),
--    Manufacturer 처럼 stringified 정수 키를 쓰도록 정렬한다.
--    bridge.corp_entity.entity_id 는 이 supplier_id 의 stringify 가 들어간다 (load_bridge 가 처리).
--
-- 2. auto.staging_relations — P3 LLM 산출의 staging.
--    Neo4j 적재는 P4 cross-validate 통과 후에만. confidence_gate 별 분류 메타 동봉.
--
-- 멱등: IF NOT EXISTS.

SET client_encoding = 'UTF8';

-- ── 1. master_suppliers ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auto.master_suppliers (
    supplier_id        BIGSERIAL       PRIMARY KEY,
    name               VARCHAR(300)    NOT NULL,
    name_norm          VARCHAR(300)    NOT NULL,
    country            VARCHAR(40),
    wikidata_qid       VARCHAR(40),
    lei                CHAR(20),
    business_no        VARCHAR(40),
    aliases            TEXT[]          NOT NULL DEFAULT '{}',
    source             VARCHAR(40)     NOT NULL,           -- 'wikidata' | 'manual' | 'ir' | ...
    source_ref         VARCHAR(200),
    confidence         NUMERIC(4,3)    NOT NULL DEFAULT 0.800,
    validated_status   VARCHAR(20)     NOT NULL DEFAULT 'candidate',
    snapshot_year      SMALLINT,
    raw                JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ     NOT NULL DEFAULT now()
);

-- 동일 supplier 가 다른 source 에서 등장할 수 있어 (qid, name_norm) 둘 다 부분 unique.
CREATE UNIQUE INDEX IF NOT EXISTS uq_auto_supplier_qid
  ON auto.master_suppliers (wikidata_qid)
  WHERE wikidata_qid IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_auto_supplier_norm ON auto.master_suppliers(name_norm);
CREATE INDEX IF NOT EXISTS idx_auto_supplier_country ON auto.master_suppliers(country);

COMMENT ON TABLE auto.master_suppliers IS
  'Tier1/2 부품 공급사 마스터. bridge.corp_entity.entity_id 가 stringified supplier_id 를 가리킨다.';
COMMENT ON COLUMN auto.master_suppliers.supplier_id IS
  'Neo4j :Supplier {entity_id} 의 SSOT — 항상 stringified.';


-- ── 2. staging_relations ─────────────────────────────────────────
-- P3 LLM 의 산출은 즉시 Neo4j 로 가지 않고 본 테이블에서 P4 결정을 기다린다.
-- merge_key 는 ExtractorEngine 의 dedupe 키와 동일 구성.
CREATE TABLE IF NOT EXISTS auto.staging_relations (
    staging_id         BIGSERIAL       PRIMARY KEY,
    -- merge 키 (한 (head, rel, tail, year) 묶음당 1 행)
    relation_type      VARCHAR(40)     NOT NULL,
    head_kind          VARCHAR(40)     NOT NULL,
    head_text_norm     VARCHAR(400)    NOT NULL,
    tail_kind          VARCHAR(40)     NOT NULL,
    tail_text_norm     VARCHAR(400)    NOT NULL,
    snapshot_year      SMALLINT,
    -- LLM 식별 결과 (가능하면 PG id 매핑)
    head_pg_id         BIGINT,
    tail_pg_id         BIGINT,
    head_text          VARCHAR(400),
    tail_text          VARCHAR(400),
    -- 산출 메타
    confidence_score   NUMERIC(4,3)    NOT NULL,
    evidence_text      TEXT,
    evidence_chunk_ids BIGINT[]        NOT NULL DEFAULT '{}',  -- 출처 청크들 (vec.chunks.id)
    extractor_name     VARCHAR(80)     NOT NULL,
    extractor_version  VARCHAR(40)     NOT NULL,
    -- P3 gate
    gate_status        VARCHAR(20)     NOT NULL,
        -- 'auto_accept' (≥0.80) | 'needs_review' (0.65~0.80) | 'rejected' (<0.65)
    -- P4 결정 (NULL = 미수행)
    p4_decision        VARCHAR(20),
        -- 'validated' | 'candidate' | 'needs_review' | 'rejected'
    p4_reason          TEXT,
    p4_at              TIMESTAMPTZ,
    -- 적재 추적
    neo4j_loaded_at    TIMESTAMPTZ,
    raw                JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ     NOT NULL DEFAULT now()
);

-- merge 키 UNIQUE — 같은 (rel, head, tail, year) 가 다시 등장하면 confidence 가 더 높은 쪽
-- 으로 UPDATE (loader 가 처리).
CREATE UNIQUE INDEX IF NOT EXISTS uq_auto_staging_merge_key
  ON auto.staging_relations
  (relation_type, head_kind, head_text_norm, tail_kind, tail_text_norm,
   COALESCE(snapshot_year, 0));

CREATE INDEX IF NOT EXISTS idx_auto_staging_gate ON auto.staging_relations(gate_status, p4_decision);
CREATE INDEX IF NOT EXISTS idx_auto_staging_rel  ON auto.staging_relations(relation_type);

COMMENT ON TABLE auto.staging_relations IS
  'P3 LLM 산출의 staging. Neo4j 적재는 P4 cross-validate 통과 후.';
