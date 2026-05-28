# 데이터 파이프라인 운영 가이드

본 문서는 AutoNexusGraph 의 raw → processed → DB 3-tier 멱등 파이프라인을 단계별로 안내한다.
각 단계는 앞 단계의 raw 가 있다면 언제든 재실행 가능 (멱등). 도중 끊겨도 `state/` 의 done/failed
체크포인트로 이어받기. 모든 적재 PG `INSERT ... ON CONFLICT DO UPDATE`, Neo4j `MERGE`.

## 전체 디렉토리 표준

```
data/
├── raw/                  ← 외부에서 받은 원본 (수정·삭제 금지)
│   ├── dart/             — corp_codes, filings, financials, structural
│   ├── krx/              — top_kospi200.csv, top_kosdaq100.csv
│   ├── wikidata/         — candidates.json, entities/<qid>.json
│   ├── wikipedia/ko/<corp_code>/ — meta.json, page.html, summary.json, infobox.json
│   ├── news/<feed>/<YYYYMMDD>/<hash>.json
│   ├── sec/<cik>/        — submissions.json
│   ├── gleif/            — kr_records.json
│   ├── kcgs/             — sample/template.csv, <year>/ratings.csv, press/<no>/
│   └── fss/, ftc/, kosis/, kipris/, law/  — 키 확보 후
│
├── processed/            ← 파싱·정규화 결과. raw 만 있으면 언제든 재생성.
│   ├── entity_resolution/
│   ├── chunks/           — 청킹 결과 (embedding NULL)
│   └── extracted/        — P3 LLM 추출 결과 (후속)
│
└── state/                ← 진행 체크포인트
    ├── ingest/<source>.done.jsonl
    └── ingest/<source>.failed.jsonl
```

## Step 별 묶음 target (Makefile)

| Step | 묶음 target | 내용 |
|---|---|---|
| 1 | `make ingest-step1` | DART corp 마스터 + KRX 상장사 + targets 매칭 |
| 2 | `make ingest-step2` | DART 사업/반기/분기 보고서 + 재무 XBRL + 정형 지배구조 |
| 3 | `make ingest-step3` | Wikidata SPARQL 한국 상장사 + 회사별 entity 상세 |
| 4 | `make ingest-step4` | Wikipedia 한국어 페이지 + Infobox |
| 5 | `make ingest-step5` | FTC 기업집단 + KOSIS + FSS (키 필요) |
| 6 | `make ingest-step6` | 연합뉴스 RSS |
| 7 | `make ingest-step7` | SEC EDGAR + GLEIF + KIPRIS + LAW |
| 8 | `make ingest-step8` | KCGS 보도자료 모니터 + 수동 CSV 적재 가이드 |

각 ingest 스크립트는 `--resume` / `--retry-failed` / `--force` / `--limit N` / `--dry-run` 옵션을 표준화.

## 추출 4-pass (PRD §6.5)

수집·청크·임베딩이 끝나면 다음 4-pass 로 관계·수치를 추출한다. P1/P2 는 deterministic
(0% LLM) — 항상 안전. P3 는 selective LLM — 비용 게이트 통과 후. P4 는 P3 결과를 P2 SSOT 로
cross-validate.

| Pass | 명령 | 입력 | 산출 | LLM |
|---|---|---|---|---|
| P1 | `make load-financials` | DART XBRL JSONL | `fin.financials` | 0% |
| P2 | `make load-graph-structural` | DART 지배구조 JSON | Neo4j SUBSIDIARY_OF / EXECUTIVE_OF / MAJOR_SHAREHOLDER_OF | 0% |
| P3 | `make p3-extract-dry` → `make p3-extract` | 사업보고서 본문 청크 | `data/processed/extracted/<corp>/<rcept>.jsonl` | 100% (selective 53%↓) |
| P4 | `make p4-load` | P3 JSONL | Neo4j PARTNER_OF / COMPETES_WITH / INVESTED_IN / PRODUCES (source=`p3_llm`) | 보조 (검증) |

**P3 비용 가드 (`extract_business_report_relations.py`):**
- `--dry-run` 이 비용 추정만 (LLM 호출 0)
- `--max-cost <USD>` HARD limit (기본 1.0 — Makefile)
- `--top-by-market-cap N` 으로 회사 수 제한 (기본 30)
- 청크당 결과는 idempotent (`data/processed/extracted/.../jsonl` 이미 있으면 skip — `--force` 로 재추출)

**P4 검증 분기 (`validator.py`):**
- `confidence >= 0.70` + P2 충돌 없음 → Neo4j MERGE
- `0.50 <= confidence < 0.70` → `data/reports/review_queue_<date>.jsonl` (사람 검토)
- `< 0.50` 또는 P2 와 충돌 → 폐기 (`ops.quality_checks` audit trail)

## LangGraph 활성화 — 에이전트 계층

데이터 적재가 끝나면 에이전트가 그 위에서 추론한다. LangGraph StateGraph + PG checkpoint
(`chat` 스키마) 가 표준. 상세는 [`agents.md`](./agents.md) 참조.

```bash
make install-agent      # pip install -e ".[agent]" — langgraph + langfuse + langsmith
make enable-langgraph   # 헬스체크: _HAS_LANGGRAPH + checkpointer 타입 확인
make serve-api          # FastAPI :31020 — POST /chat (blocking) + /chat/stream (SSE)
make serve-ui           # Streamlit :31021 — st.status 노드 진행 표시
```

체크포인트 테이블은 자동 생성 (`chat.checkpoints`, `chat.checkpoint_writes`,
`chat.checkpoint_blobs`, `chat.checkpoint_migrations`). 스키마 위치는
`.env` 의 `LANGGRAPH_CHECKPOINT_SCHEMA` 로 변경 가능 (기본 `chat`).

## 적재 순서 — 의존성 (DAG)

```
ingest-corp     → load-companies      ─┐
ingest-krx      ────────────────────────┤→ load-entity-map (시드)
ingest-targets  ────────────────────────┘

ingest-bulk     → load-filings, load-financials
ingest-structural → load-graph-structural, load-persons

ingest-wikidata → load-wikidata        — entity_map 보강 (QID/ISIN/LEI/CIK/homepage)
ingest-wikipedia → load-wikipedia, build-wiki-chunks

ingest-news     → load-news, load-graph-news

ingest-sec      → load-sec
ingest-gleif    → load-gleif           — entity_map 보강 (LEI)

make migrate-schema                    — 1회 (Sector→Industry / Person birth_year)
make validate-quality                  — 마지막에 매번 실행
```

## 자주 쓰는 명령

```bash
# 인프라
make up                       # PG + Neo4j docker-compose up
make health                   # 모든 컴포넌트 ping

# 수집 (점진 — 이어받기)
make ingest-step1             # 마스터부터 시작

# 적재 (멱등)
make load-companies load-entity-map

# 임베딩 (장시간)
make serve-embeddings &       # 별도 프로세스 권장
make embed-chunks             # vec.chunks NULL embedding backfill

# 품질 검증
make validate-quality         # → data/reports/quality_<date>.md
```

## 재처리 시나리오

### 1) 청킹 로직 수정 후 재청크
```bash
# 청크 메타만 갱신 — raw 는 그대로
rm -rf data/processed/chunks
psql $POSTGRES_DSN -c "TRUNCATE vec.chunks RESTART IDENTITY"
make build-chunks build-wiki-chunks
make embed-chunks
```

### 2) 임베딩 모델 교체 (BGE-M3 → 다른 모델)
```bash
# embedding 만 NULL 화. text/메타는 유지
psql $POSTGRES_DSN -c "UPDATE vec.chunks SET embedding = NULL"
# 새 모델 가동 후
make embed-chunks
```

### 3) 그래프 스키마 정합성 (라벨/관계명 충돌)
```bash
make migrate-schema                   # 멱등. 변경 0 이면 이미 적용됨.
```

### 4) Entity Resolution 매핑 보강 (신규 외부 소스)
```bash
# 신규 source 추가했으면 적재 후
make load-entity-map          # 시드 (DART 자체 ID)
make load-wikidata            # QID / LEI / ISIN / CIK 추가
make load-gleif               # LEI 보강
make validate-quality         # 매핑 커버리지 점검
```

## 라이선스 정책 (자동 강제)

`src/autonexusgraph/ingestion/_license.py` 의 `LICENSE_POLICY` 가 source 키별 본문 저장 여부를 강제.
`save_raw()` 호출 시 정책 확인 → `copyrighted` / `metadata_only` 이면 본문 필드 자동 strip.

| Tier | 예시 source | 본문 저장 |
|---|---|---|
| public_domain | dart, sec_edgar, kosis | OK |
| cc0 | wikidata | OK |
| cc_by_sa | wikipedia | OK (출처표기) |
| cc_by_4_0 | gleif | OK (출처표기) |
| kogl_type1 | fss_press, ftc, kipris | OK |
| copyrighted | news_yonhap, news_hankyung | 제목+요약+URL 만 |
| metadata_only | bigkinds | 본문 X |

## 점검 쿼리

```sql
-- PG 적재량 한눈에
SELECT 'master.companies' tbl, count(*) FROM master.companies UNION ALL
SELECT 'entity_map',          count(*) FROM master.entity_map UNION ALL
SELECT 'persons',              count(*) FROM master.persons UNION ALL
SELECT 'vec.chunks',           count(*) FROM vec.chunks UNION ALL
SELECT 'vec.chunks (embedded)',count(*) FROM vec.chunks WHERE embedding IS NOT NULL;

-- ID 커버리지
SELECT id_type, count(*) FROM master.entity_map GROUP BY 1 ORDER BY 2 DESC;
```

```cypher
// Neo4j 상태
MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c ORDER BY c DESC;
MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS c ORDER BY c DESC;

// 동명이인 분리 검증
MATCH (p:Person) WITH p.name AS name, collect(DISTINCT p.birth_year) AS years
WHERE size(years) > 1 RETURN name, years LIMIT 10;
```
