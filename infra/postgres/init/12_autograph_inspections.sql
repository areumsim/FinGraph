-- AutoGraph — 자동차검사관리 수리검사내역 (data.go.kr 15155857).
--
-- KOTSA (한국교통안전공단) 의 사고·침수·도난 차량 수리검사 결과.
-- NHTSA recalls 와 별개 — 차종 단위가 아니라 VIN(차대번호) 단위 검사 이력.
--
-- 멱등 — IF NOT EXISTS.

CREATE SCHEMA IF NOT EXISTS auto;

CREATE TABLE IF NOT EXISTS auto.events_inspections (
    inspection_id        BIGSERIAL PRIMARY KEY,
    source               VARCHAR NOT NULL DEFAULT 'datagokr_kotsa',
    source_inspection_id VARCHAR,                       -- KOTSA 검사번호
    vin                  VARCHAR,                        -- 차대번호 (개인식별이라 마스킹 가능)
    manufacturer_id      BIGINT REFERENCES auto.master_manufacturers(manufacturer_id),
    model_id             BIGINT REFERENCES auto.master_vehicle_models(model_id),
    variant_id           BIGINT REFERENCES auto.master_vehicle_variants(variant_id),

    inspection_type      VARCHAR,                        -- 사고 / 침수 / 도난 / 정기 / ...
    result               VARCHAR,                        -- 적합 / 부적합 / ...
    inspected_at         DATE,
    reason               TEXT,                           -- 검사 사유 자유 텍스트
    snapshot_year        SMALLINT NOT NULL DEFAULT EXTRACT(YEAR FROM now())::SMALLINT,
    raw                  JSONB,
    ingested_at          TIMESTAMP NOT NULL DEFAULT now(),

    UNIQUE (source, source_inspection_id)
);

CREATE INDEX IF NOT EXISTS idx_inspections_mfr
    ON auto.events_inspections(manufacturer_id);
CREATE INDEX IF NOT EXISTS idx_inspections_model
    ON auto.events_inspections(model_id);
CREATE INDEX IF NOT EXISTS idx_inspections_year
    ON auto.events_inspections(snapshot_year);
CREATE INDEX IF NOT EXISTS idx_inspections_vin
    ON auto.events_inspections(vin);
