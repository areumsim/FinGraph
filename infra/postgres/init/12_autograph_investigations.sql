-- AutoGraph 조사 (NHTSA ODI Investigations) 이벤트 테이블.
--
-- 리콜 전 단계의 NHTSA 결함 조사 — 결함 시계열 / 잠재적 리콜 신호.
-- 출처: https://static.nhtsa.gov/odi/ffdd/inv/FLAT_INV.zip (daily, TAB-delimited)
--
-- 멱등: IF NOT EXISTS. 본 마이그레이션은 events_recalls 와 독립.

SET client_encoding = 'UTF8';

-- ── 조사 이벤트 ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auto.events_investigations (
    investigation_id        BIGSERIAL       PRIMARY KEY,
    source                  VARCHAR(40)     NOT NULL DEFAULT 'nhtsa_odi',
    action_number           VARCHAR(20)     NOT NULL,        -- e.g., 'PE12001', 'EA22002'
    investigation_type      VARCHAR(8),                       -- 'PE'/'EA'/'RQ'/'AQ'/'DP' (action_number 첫 2글자)
    manufacturer_id         BIGINT          REFERENCES auto.master_manufacturers(manufacturer_id),
    model_id                BIGINT          REFERENCES auto.master_vehicle_models(model_id),
    variant_id              BIGINT          REFERENCES auto.master_vehicle_variants(variant_id),
    mfr_name                VARCHAR(80),                      -- 원문 (정규화 전)
    component_text          VARCHAR(400),                     -- 원문 부품 표기
    component_id            BIGINT          REFERENCES auto.components(component_id),
    opened_date             DATE,
    closed_date             DATE,
    campno                  VARCHAR(20),                      -- 연관 리콜 캠페인 #
    subject                 VARCHAR(400),                     -- 1줄 요약
    summary                 TEXT,                             -- 본문 (최대 6000자)
    country                 VARCHAR(8)      DEFAULT 'US',
    confidence              NUMERIC(4,3)    NOT NULL DEFAULT 0.950,
    validated_status        VARCHAR(20)     NOT NULL DEFAULT 'verified',
    snapshot_year           SMALLINT,
    raw                     JSONB           NOT NULL DEFAULT '{}'::jsonb,
    ingested_at             TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (source, action_number)
);

CREATE INDEX IF NOT EXISTS idx_auto_inv_var       ON auto.events_investigations(variant_id);
CREATE INDEX IF NOT EXISTS idx_auto_inv_model     ON auto.events_investigations(model_id);
CREATE INDEX IF NOT EXISTS idx_auto_inv_mfr_date  ON auto.events_investigations(manufacturer_id, opened_date DESC);
CREATE INDEX IF NOT EXISTS idx_auto_inv_type      ON auto.events_investigations(investigation_type);
CREATE INDEX IF NOT EXISTS idx_auto_inv_campno    ON auto.events_investigations(campno) WHERE campno IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_auto_inv_open_year ON auto.events_investigations(snapshot_year);
-- 본문 LIKE 검색 보조 (소문자).
CREATE INDEX IF NOT EXISTS idx_auto_inv_subj_lower
  ON auto.events_investigations((lower(subject)))
  WHERE subject IS NOT NULL;

COMMENT ON TABLE  auto.events_investigations IS 'NHTSA ODI 결함 조사 (리콜 전단계). NHTSA_ACTION_NUMBER 단위.';
COMMENT ON COLUMN auto.events_investigations.investigation_type IS
  'PE=Preliminary Evaluation, EA=Engineering Analysis, RQ=Recall Query, AQ=Audit Query, DP=Defect Petition';
COMMENT ON COLUMN auto.events_investigations.campno IS '조사가 리콜로 종결된 경우 그 캠페인 번호.';
