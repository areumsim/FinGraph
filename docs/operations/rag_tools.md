# RAG / 에이전트 도구 가이드

`src/autonexusgraph/tools/` 의 사전 정의 함수들은 LLM 에이전트가 호출하는 인터페이스다.
자유 SQL/Cypher/벡터 호출은 모두 금지(PRD §7.5.10). 함수명 + 파라미터만 LLM 이 결정.

## 모듈 구성

| 모듈 | 역할 | 백엔드 |
|---|---|---|
| `tools.financials` | 재무 수치·회사 마스터 | PostgreSQL |
| `tools.graph` | 자회사·임원·주주·뉴스·기업집단 | Neo4j (via `tools/cypher_templates.py` 레지스트리) |
| `tools.retrieve` | 본문 의미 검색 + 메타 필터 | pgvector |
| `tools.cypher_templates` | Cypher 템플릿 + 파라미터 JSON Schema 검증 (PRD §7.5.9) | — |

모든 함수는 dict / list[dict] 반환 (JSON serializable). Neo4j 호출은 다중 가드:

1. **레지스트리**: `cypher_templates.render_template(name, params)` 가 type / range / regex 검증 — 실패 시 `TemplateError`
2. **cypher_guard**: `_run()` 직전 정적 READ-ONLY 검사 — CREATE/MERGE/DELETE/APOC write 차단
3. **그래프 폭발 가드**: `_cap(limit)` 가 `HARD_LIMIT=500` 초과 차단

## tools.cypher_templates 레지스트리 (PRD §7.5.9)

총 22개 템플릿 등록 (`list_templates()` 로 조회). 동적 부분 (hops 1~5, depth 1~3) 은
사전 등록된 변형으로 처리 — Cypher 문자열 인라인 포매팅 일체 금지.

`param_schema` 예시:
```python
"list_subsidiaries": {
    "cypher": "MATCH ... WHERE $year IS NULL OR r.rcept_year = $year ...",
    "required_params": ["cc", "limit"],
    "param_schema": {
        "cc": (str, ("regex", r"^\d{8}$")),           # corp_code 8자리
        "year": (int, ("range", 1990, 2099)),         # optional (None 허용)
        "limit": (int, ("range", 1, 500)),
    },
}
```

지원 constraint: `("enum", set)`, `("range", min, max)`, `("regex", pattern)`. bool 은
int/float 자리에서 거절 (의도치 않은 truthy 통과 방지).

## tools.graph 주요 함수

### 회사 식별
```python
lookup_company(query: str, limit: int = 5) -> list[dict]
```
이름·종목코드·corp_code 어떤 입력도 매칭. Wikidata QID / Wikipedia title 도 반환 → 다음 호출에 활용.

### 인물 식별 (동명이인 분리)
```python
lookup_person(name: str, birth_year: int | None = None, limit: int = 5) -> list[dict]
```
birth_year 지정하면 정확 매칭, 미지정이면 모든 동명이인 + 각자 임원직 sample 5개씩.

### 자회사 그래프
```python
list_subsidiaries(parent_corp_code, include_related=False, snapshot_year=None, limit=50)
list_parents(child_corp_code_or_name, limit=50)
```
`include_related=True` 면 관계회사(5~50%) 까지. `snapshot_year` 지정 가능.

### 임원
```python
get_executives(corp_code, role_contains=None, snapshot_year=None, limit=50)
get_companies_of_person(name, birth_year=None, role_contains=None, limit=50)
```
`role_contains='대표'` 면 DART `r.role` 의 등기구분 ('사내이사') 또는 `r.duty` 의 자유텍스트
('대표이사') 양쪽에서 substring 매칭 → 진짜 CEO 만 추림.

### 최대주주
```python
get_major_shareholders(corp_code, min_pct=0.0, snapshot_year=None, limit=50)
```
자연인 + 법인 모두 반환. `labels(h)[0]` 로 `Person` / `Company` 구분.

### 멀티홉 탐색
```python
find_paths(start_corp_code, end_corp_code, max_hops=3) -> list[dict]
```
두 회사 간 최단 경로. SUBSIDIARY_OF / EXECUTIVE_OF / MAJOR_SHAREHOLDER_OF / MENTIONS 등 모든
관계 통합 탐색.

```python
get_subgraph(corp_code, depth=1, limit_nodes=50) -> dict
```
중심 노드 기준 depth 이내 노드/엣지. UI 시각화 + 컨텍스트 묶음용.

### 뉴스 / 그룹
```python
list_mentioning_news(corp_code, limit=50)         # 이 회사 언급된 뉴스
list_cooccurring(corp_code, min_count=2)          # 공동 언급 회사 쌍
list_group_members(group_name)                    # 공정위 기업집단 계열사
```

## tools.retrieve — Hybrid 검색

### 의미 검색 + 메타 필터
```python
search_documents(
    query: str,
    *,
    top_k: int = 8,
    corp_code: str | list[str] | None = None,
    fiscal_year: int | None = None,
    fiscal_year_min: int | None = None,
    fiscal_year_max: int | None = None,
    source: str | list[str] | None = None,      # 'dart', 'wikipedia', ...
    section_contains: str | None = None,         # '위험', '사업' 등
    report_type: str | None = None,              # 'annual_business' 등
) -> list[dict]
```
리턴: `id, corp_code, rcept_no, source, section, report_type, fiscal_year, chunk_idx, text, token_count, score`
(score = 1 - cosine distance, 0~1)

### 결정적 fetch (벡터 미사용)
```python
search_by_metadata(corp_code=…, fiscal_year=…, section_contains=…, source=…, limit=50)
```
"삼성전자 2024년 사업보고서 위험요인 섹션 전체" 같은 결정적 페치 시.

### 단일 청크
```python
get_chunk(chunk_id: int) -> dict | None
```

## 시나리오 예시

### 1) "삼성전자 2024년 위험요인을 요약해줘"
```python
chunks = search_documents(
    "주요 사업 위험요인",
    corp_code="00126380",
    fiscal_year=2024,
    section_contains="위험",
    report_type="annual_business",
    top_k=5,
)
# → 5개 청크 텍스트를 LLM 에 프롬프트로 주입
```

### 2) "삼성전자 자회사 중 ESG 등급 A+ 인 곳"
```python
subs = list_subsidiaries("00126380", limit=200)
# 각 자회사 corp_code 로 PG esg.ratings 조회
```

### 3) "이재용이 임원인 회사의 영업이익 합"
```python
from autonexusgraph.tools.financials import get_operating_income
companies = get_companies_of_person("이재용")
total = 0
for c in companies:
    if not c["corp_code"]:
        continue
    r = get_operating_income(c["corp_code"], 2024)
    if r:
        total += r["value"]
```

### 4) "삼성전자 vs SK하이닉스 — 어떻게 연결돼있나"
```python
paths = find_paths("00126380", "00164779", max_hops=3)
# → [{"node_path": [...], "rel_types": [...], "hops": 2}]
```

### 5) "최근 1년 부정 뉴스 많은 회사 — 삼성전자 자회사 중"
```python
subs = list_subsidiaries("00126380")
for s in subs:
    if not s.get("child_corp_code"):
        continue
    news = list_mentioning_news(s["child_corp_code"], limit=20)
    # news 분석 (감성 추출은 향후 P3)
```

## 안전 가드 (자동 적용)

- 모든 함수에 `limit` 인자 + 내부 `HARD_LIMIT` (그래프 500, 벡터 50)
- `search_documents` 는 `embedding IS NOT NULL` 자동 필터 (백필 중에도 안전)
- `lookup_person` 의 birth_year 미지정 시 모든 동명이인 반환 (사용자 선택)
- SQL 은 named placeholder (`%(key)s`), Cypher 는 named param (`$key`) — 인젝션 방지

## 임베딩 서버

`tools.retrieve.search_documents` 는 BGE-M3 HTTP 서버(EMBEDDING_URL) 호출.

```bash
make serve-embeddings              # 별도 터미널, CUDA_VISIBLE_DEVICES=0
# → http://127.0.0.1:8080  (BGE-M3 1024d cosine)
```

서버 미가동 시 `search_documents` 는 RuntimeError 발생. `search_by_metadata` 는 벡터 호출 안 하므로
임베딩 서버 없이도 동작.
