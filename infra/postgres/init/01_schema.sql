-- FinGraph PostgreSQL 초기 스키마
-- docker compose up 시 /docker-entrypoint-initdb.d/ 가 자동 실행.
--
-- 구획:
--   1. master: 회사 마스터 (DART corp_codes + KRX 보강)
--   2. financials: 재무제표 (XBRL fnlttSinglAcntAll)
--   3. filings: 공시 보고서 메타
--   4. macro: 거시지표 (ECOS)
--   5. chat: 대화 히스토리 (PRD §7.6)
--   6. eval: 평가 QA + 결과
--   7. vec: 문서 청크 벡터 (pgvector — Qdrant 대체)

SET client_encoding = 'UTF8';

-- ── 0. 확장 ────────────────────────────────────────────────────────
-- pgcrypto: gen_random_uuid()
-- vector:   pgvector (청크 임베딩 저장·검색)
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- ── 1. 회사 마스터 ─────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS master;

CREATE TABLE IF NOT EXISTS master.companies (
    corp_code      CHAR(8)         PRIMARY KEY,         -- DART 고유 8자리
    corp_name      VARCHAR(200)    NOT NULL,
    stock_code     CHAR(6),                              -- 상장사만 (6자리)
    market         VARCHAR(20),                          -- KOSPI/KOSDAQ/KONEX
    sector         VARCHAR(100),
    industry       VARCHAR(100),
    listed_at      DATE,
    is_active      BOOLEAN         NOT NULL DEFAULT TRUE,
    extra          JSONB           NOT NULL DEFAULT '{}'::jsonb,
    updated_at     TIMESTAMPTZ     NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_companies_stock ON master.companies(stock_code);
CREATE INDEX IF NOT EXISTS idx_companies_name  ON master.companies(corp_name);

CREATE TABLE IF NOT EXISTS master.index_constituents (
    index_name     VARCHAR(50)     NOT NULL,             -- KOSPI200, KOSDAQ150
    stock_code     CHAR(6)         NOT NULL,
    snapshot_date  DATE            NOT NULL,
    weight         NUMERIC(8, 4),
    PRIMARY KEY (index_name, stock_code, snapshot_date)
);

-- ── 2. 재무제표 ────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS fin;

CREATE TABLE IF NOT EXISTS fin.financials (
    id             BIGSERIAL       PRIMARY KEY,
    corp_code      CHAR(8)         NOT NULL REFERENCES master.companies(corp_code),
    bsns_year      SMALLINT        NOT NULL,             -- 사업연도
    reprt_code     CHAR(5)         NOT NULL,             -- 11011=사업, 11012=반기...
    fs_div         CHAR(3)         NOT NULL,             -- CFS=연결, OFS=별도
    sj_div         VARCHAR(10)     NOT NULL,             -- BS/IS/CIS/CF/SCE
    account_id     VARCHAR(100),                          -- 표준 계정 ID (XBRL)
    account_nm     VARCHAR(200)    NOT NULL,
    thstrm_amount  NUMERIC(28, 0),                       -- 당기 (KRW)
    frmtrm_amount  NUMERIC(28, 0),
    bfefrmtrm_amount NUMERIC(28, 0),
    ord            INT,
    raw            JSONB,                                 -- 원본 row (감사용)
    UNIQUE (corp_code, bsns_year, reprt_code, fs_div, account_id, account_nm)
);
CREATE INDEX IF NOT EXISTS idx_fin_corp_year ON fin.financials(corp_code, bsns_year);
CREATE INDEX IF NOT EXISTS idx_fin_account   ON fin.financials(account_nm);

-- ── 3. 공시 보고서 메타 ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fin.filings (
    rcept_no       CHAR(14)        PRIMARY KEY,          -- DART 접수번호
    corp_code      CHAR(8)         NOT NULL REFERENCES master.companies(corp_code),
    report_nm      VARCHAR(300)    NOT NULL,
    rcept_dt       DATE            NOT NULL,
    flr_nm         VARCHAR(200),
    pblntf_ty      CHAR(1),                              -- A=정기, B=주요사항, ...
    raw            JSONB,
    ingested_at    TIMESTAMPTZ     NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_filings_corp_date ON fin.filings(corp_code, rcept_dt DESC);

-- ── 4. 거시지표 (ECOS) ────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS macro;

CREATE TABLE IF NOT EXISTS macro.series (
    stat_code      VARCHAR(20)     NOT NULL,
    item_code      VARCHAR(40)     NOT NULL,
    time           VARCHAR(20)     NOT NULL,             -- D=YYYYMMDD, M=YYYYMM, Q=YYYYQN, A=YYYY
    cycle          CHAR(2)         NOT NULL,
    value          NUMERIC(28, 6),
    unit           VARCHAR(40),
    stat_name      VARCHAR(300),
    PRIMARY KEY (stat_code, item_code, time)
);
CREATE INDEX IF NOT EXISTS idx_macro_stat_time ON macro.series(stat_code, time);

-- ── 5. 채팅 / 대화 히스토리 (PRD §7.6) ────────────────────────────
CREATE SCHEMA IF NOT EXISTS chat;

CREATE TABLE IF NOT EXISTS chat.conversations (
    id             UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id      VARCHAR(64)     NOT NULL UNIQUE,      -- LangGraph thread_id 와 1:1
    title          VARCHAR(200)    NOT NULL DEFAULT 'New conversation',
    user_id        VARCHAR(100),
    created_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
    metadata       JSONB           NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_conv_user_updated ON chat.conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS chat.messages (
    id             BIGSERIAL       PRIMARY KEY,
    conversation_id UUID           NOT NULL REFERENCES chat.conversations(id) ON DELETE CASCADE,
    turn_idx       INT             NOT NULL,             -- conversation 내 0,1,2,...
    role           VARCHAR(20)     NOT NULL,             -- user | assistant | system
    content        TEXT            NOT NULL,
    citations      JSONB           NOT NULL DEFAULT '[]'::jsonb,
    visualizations JSONB           NOT NULL DEFAULT '[]'::jsonb,
    agent_trace    JSONB           NOT NULL DEFAULT '{}'::jsonb,    -- 어떤 에이전트 호출, 토큰, 비용
    created_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, turn_idx, role)
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON chat.messages(conversation_id, turn_idx);

-- 전문 검색 (한국어는 simple, 향후 PG `mecab-ko` 검토)
ALTER TABLE chat.messages
  ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED;
CREATE INDEX IF NOT EXISTS idx_msg_tsv ON chat.messages USING GIN(content_tsv);

-- ── 6. 평가 (PRD §8) ──────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS eval;

CREATE TABLE IF NOT EXISTS eval.qa_gold (
    id             VARCHAR(50)     PRIMARY KEY,
    question       TEXT            NOT NULL,
    answer         TEXT            NOT NULL,
    sources        JSONB           NOT NULL DEFAULT '[]'::jsonb,
    level          SMALLINT        NOT NULL,             -- 1=fact, 2=2hop, 3=3hop+
    expected_hops  SMALLINT,
    tags           TEXT[]          NOT NULL DEFAULT '{}',
    created_at     TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval.runs (
    id             UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    system         VARCHAR(50)     NOT NULL,             -- vector / graph / hybrid / sql_vec
    llm_provider   VARCHAR(20)     NOT NULL,
    llm_model      VARCHAR(100)    NOT NULL,
    qa_id          VARCHAR(50)     NOT NULL REFERENCES eval.qa_gold(id),
    predicted      TEXT,
    is_correct     BOOLEAN,
    judge_score    NUMERIC(5, 2),
    latency_sec    NUMERIC(8, 3),
    tokens         INT,
    cost_usd       NUMERIC(10, 6),
    trace          JSONB,
    created_at     TIMESTAMPTZ     NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_runs_system_qa ON eval.runs(system, qa_id);

-- ── 7. 벡터 저장 (pgvector — Qdrant 대체) ───────────────────────────
CREATE SCHEMA IF NOT EXISTS vec;

-- 청크 단위 본문 + 임베딩.
-- dim=1024 는 BGE-M3 dense 차원. 모델 바뀌면 별 테이블 또는 ALTER 필요.
CREATE TABLE IF NOT EXISTS vec.chunks (
    id             BIGSERIAL       PRIMARY KEY,
    corp_code      CHAR(8)         NOT NULL REFERENCES master.companies(corp_code),
    rcept_no       CHAR(14)        REFERENCES fin.filings(rcept_no),
    section        VARCHAR(100),                          -- 사업개요/위험요인/지배구조 등
    chunk_idx      INT             NOT NULL,              -- 보고서 내 순번
    text           TEXT            NOT NULL,
    token_count    INT,
    embedding      vector(1024),                          -- BGE-M3 dim
    metadata       JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ     NOT NULL DEFAULT now(),
    UNIQUE (rcept_no, chunk_idx)
);

-- ANN 인덱스 (HNSW — pgvector 0.5+). lists 보다 빠름. 코사인 거리.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
  ON vec.chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_chunks_corp_section ON vec.chunks(corp_code, section);
