-- AutoGraph 글로벌 OEM SEC EDGAR 재무 확장.
--
-- SEC EDGAR Company Facts API (https://data.sec.gov/api/xbrl/companyfacts/CIK*.json)
-- 가 제공하는 XBRL 정제 facts 를 글로벌 OEM 단위로 저장.
--
-- bridge.corp_entity 에 sec_cik 컬럼 추가 → manufacturer_id ↔ SEC CIK 매핑.
-- 한국 OEM (Hyundai/Kia/Genesis/KGM) 은 SEC 미발행 → DART (master.entity_map) 사용.
-- 글로벌 OEM (Tesla/Ford/GM/Stellantis/Toyota ADR …) 은 SEC CIK 가 진입점.
--
-- 멱등: IF NOT EXISTS / ADD COLUMN IF NOT EXISTS.

SET client_encoding = 'UTF8';

-- ── 1. bridge.corp_entity 확장 — SEC CIK 컬럼 ────────────────
ALTER TABLE bridge.corp_entity
  ADD COLUMN IF NOT EXISTS sec_cik VARCHAR(10);

-- (sec_cik, entity_type) 단위 unique — 같은 CIK 가 동일 entity_type 으로 두 번 등록되지 않게.
CREATE UNIQUE INDEX IF NOT EXISTS uq_bridge_sec_cik
  ON bridge.corp_entity (sec_cik, entity_type)
  WHERE sec_cik IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bridge_sec_cik
  ON bridge.corp_entity (sec_cik) WHERE sec_cik IS NOT NULL;

COMMENT ON COLUMN bridge.corp_entity.sec_cik IS
  'SEC EDGAR Central Index Key (10-digit). 글로벌 OEM cross-domain 진입점 — corp_code(DART) 와 양자택일·또는 양쪽 동시 보유.';


-- ── 2. auto.oem_financials_sec — XBRL 정제 facts ─────────────
-- SEC Company Facts 의 us-gaap / dei 네임스페이스 fact 를 long format 으로 저장.
CREATE TABLE IF NOT EXISTS auto.oem_financials_sec (
    id                  BIGSERIAL       PRIMARY KEY,
    manufacturer_id     BIGINT          REFERENCES auto.master_manufacturers(manufacturer_id),
    sec_cik             VARCHAR(10)     NOT NULL,
    taxonomy            VARCHAR(20)     NOT NULL,        -- 'us-gaap' | 'dei' | 'ifrs-full'
    concept             VARCHAR(120)    NOT NULL,        -- 'Revenues', 'NetIncomeLoss', 'ResearchAndDevelopmentExpense', ...
    unit                VARCHAR(20)     NOT NULL,        -- 'USD' | 'shares' | 'pure'
    fiscal_year         SMALLINT,                         -- fy
    fiscal_period       VARCHAR(8),                       -- 'FY' | 'Q1' | 'Q2' | 'Q3'
    period_end          DATE,                             -- end date
    period_start        DATE,                             -- (선택) duration fact 의 start
    value               NUMERIC(28, 2)  NOT NULL,
    form_type           VARCHAR(20),                      -- '10-K' | '10-Q' | '20-F' | …
    accession_no        VARCHAR(20),                      -- 'xxxxxxxxxx-xx-xxxxxx'
    filed_at            DATE,
    confidence          NUMERIC(4,3)    NOT NULL DEFAULT 0.950,
    validated_status    VARCHAR(20)     NOT NULL DEFAULT 'verified',
    raw                 JSONB           NOT NULL DEFAULT '{}'::jsonb,
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT now(),
    -- 같은 (CIK, concept, fy, fiscal_period, unit, form_type) 단위 unique.
    -- 같은 fact 가 amended 10-K/A 로 다시 출현하면 form_type 으로 분리 보존.
    UNIQUE (sec_cik, concept, fiscal_year, fiscal_period, unit, form_type)
);

CREATE INDEX IF NOT EXISTS idx_auto_oem_sec_mfr        ON auto.oem_financials_sec(manufacturer_id, fiscal_year DESC);
CREATE INDEX IF NOT EXISTS idx_auto_oem_sec_cik_year   ON auto.oem_financials_sec(sec_cik, fiscal_year DESC);
CREATE INDEX IF NOT EXISTS idx_auto_oem_sec_concept    ON auto.oem_financials_sec(concept);
CREATE INDEX IF NOT EXISTS idx_auto_oem_sec_period_end ON auto.oem_financials_sec(period_end DESC);

COMMENT ON TABLE  auto.oem_financials_sec IS
  '글로벌 OEM (Tesla/Ford/GM/Stellantis/Toyota ADR …) 의 SEC XBRL facts. KRW finance 와 통화 분리.';
COMMENT ON COLUMN auto.oem_financials_sec.concept IS
  'XBRL concept name (예: us-gaap:Revenues, us-gaap:NetIncomeLoss, us-gaap:ResearchAndDevelopmentExpense). taxonomy prefix 분리.';
COMMENT ON COLUMN auto.oem_financials_sec.fiscal_period IS
  'FY=연간, Q1/Q2/Q3=분기. SEC Company Facts 의 fp 값 그대로.';
