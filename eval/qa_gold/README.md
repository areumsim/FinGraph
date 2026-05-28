# qa_gold — 평가용 정답 데이터셋

PRD v2.1 §8.1 의 도메인 내 100문항 + Cross-Domain 30문항 평가셋 큐레이션 가이드.

세 파일:

| 파일 | 도메인 | 목표 row | 현재 row | 비고 |
|---|---|---:|---:|---|
| `gold_qa_v0.jsonl` | finance | 100 (L1 30 / L2 40 / L3 30) | seed 30 | 코스피200+코스닥100 기반 |
| `gold_qa_auto_v0.jsonl` | auto | 100 (L1 30 / L2 40 / L3 30) | seed 42 | NHTSA + Wikidata 기반 |
| `gold_qa_cross_v0.jsonl` | cross_domain | 30 (CD-L1 10 / CD-L2 8 / CD-L3 8 / CD-L4 4) | 30 | Bridge 4 한국 OEM/부품사 |
| `gold_qa_v0.example.jsonl` | finance | (테스트 픽스처) | 3 | 패키지 동작 검증용 — 데이터 의미 없음 |

> 본 디렉토리의 jsonl 은 **실제 DB 조회로 검증 가능한 정답만 포함**한다. 예측·추정 정답
> 또는 모델 자가생성 정답은 큐레이션 정책 위반. `notes` 에 데이터 가정·전제 명시.

---

## 1. 스키마 (PRD §8.1 확장)

```json
{
  "qid":                   "Q0001",
  "question":              "현대모비스 2023년 매출은?",
  "question_type":         "single_entity",
                              // single_entity | multi_entity | relation
                              // | aggregation | ranking | comparison
  "complexity":            "easy",            // easy | medium | hard
  "requires_multi_hop":    false,             // PRD §2.2 multi-hop 75%+ 측정
  "hop_count":             1,                 // 그래프 hop 수 (정량)
  "domain":                "finance",         // finance | auto | cross_domain
  "level":                 "L1",              // L1 | L2 | L3 (도메인 내) — 또는
                                              // CD-L1 | CD-L2 | CD-L3 | CD-L4 (cross)

  "gold_answer_text":      ["7조 1,261억원", "약 7조 1천억원"],
                                              // paraphrase 허용. EM/F1 의 max
  "gold_answer_entities":  ["현대모비스", "매출"],

  "evidence_doc_ids":      ["rcept_no_or_chunk_ids"],   // 옵션 (Recall@k)
  "evidence_corp_codes":   ["00164788"],

  "gold_cypher":           null,              // 옵션 (execution_accuracy)
  "scenario_id":           null,              // 옵션 — 시나리오 집계 키
  "tags":                  ["sql_only", "revenue"],

  // PRD §8.1 추가 메타.
  "required_stores":         ["AutoNexusGraph.SQL"],
                              // 어느 저장소가 풀이에 필요한가
                              // AutoNexusGraph.SQL / AutoNexusGraph.Graph
                              // AutoGraph.SQL / AutoGraph.Graph / AutoGraph.Vector
                              // Bridge
  "required_confidence_min": 0.7,             // 답변 근거 엣지 confidence 최소
  "main_hop_path":           ["Company"],     // 메인 홉 경로 (PRD §4.4 / §10.13)
  "side_hops":               [],              // 보조 홉 (Standard / Plant / Supplier ...)
  "source_citations":        [],              // 정답을 직접 뒷받침하는 chunk_id / row_id

  "is_answerable":         true,              // false 면 refusal 평가
  "notes":                 ""
}
```

### 필드 정책

- `qid` 는 prefix 로 도메인·레벨 식별: `FIN-L1-001`, `AUTO-L2-001`, `CD-L1-001`.
- `gold_answer_text` 는 paraphrase 3개 이상 권장 — `em`/`f1` 의 max 매칭.
- `gold_answer_entities` 는 Hits@k 매칭 — 정확한 표기 + alias.
- `evidence_doc_ids` 가 있으면 `recall@k` 평가 가능.
- `gold_cypher` 가 있으면 `execution_accuracy` 평가 가능 — `MATCH ... RETURN ...` 만.
- `is_answerable=false` 행은 refusal precision 측정용 — DB 에 없는 사실로 의도적으로 작성.

---

## 2. 큐레이션 가이드

### 2.1 finance (PRD §8.1)

| Level | 비율 | 형태 | 예시 |
|---|---:|---|---|
| **L1** factual | 30% | 단일 호출 (`get_revenue` / `get_executives` / `lookup_company`) | "삼성전자 2024년 매출은?" |
| **L2** structural | 40% | 2-hop (graph + financials, 또는 graph + graph) | "삼성전자 자회사 중 매출 1조 이상은?" |
| **L3** narrative + multi-step | 30% | 3-hop+ (graph + financials + vector) | "이재용이 임원인 회사들의 합산 영업이익은?" |

도메인 분포 권장:
- 30% factual 수치 (매출/영업이익/순이익/자산)
- 40% structural (자회사/임원/주주/그룹)
- 30% narrative (위험요인/사업개요 — 의미 검색)

### 2.2 auto (PRD §8.1 + AutoGraph 도메인)

| Level | 비율 | 형태 | 예시 |
|---|---:|---|---|
| **L1** spec | 30% | `get_spec` 단일 호출 | "현대 그랜저 2024 변속기는?" |
| **L2** recall/complaint | 40% | recall ↔ variant, complaint ↔ variant | "쏘나타 DN8 에어백 리콜 사례는?" |
| **L3** supply chain | 30% | supplier ↔ module ↔ vehicle ↔ recall 3+ hop | "현대모비스 부품을 쓰는 차종 중 리콜?" |

### 2.3 cross_domain (PRD §8.1 §10.8 — 4단계 층화)

| 난이도 | 정의 | 문항 수 | 목표 정답률 | 예시 |
|---|---|---:|---:|---|
| **CD-L1** | 제조사 ↔ 상장사 직접 Bridge | 10 | 80%+ | "현대차가 제조한 모델의 리콜 건수와 현대차 영업이익?" |
| **CD-L2** | 차량 모델 ↔ 제조사 ↔ 재무 | 8 | 70%+ | "쏘나타 DN8 을 만드는 회사의 최근 3년 영업이익 추이?" |
| **CD-L3** | 부품/공급사 ↔ OEM ↔ 재무 | 8 | 50~60% | "현대모비스 부품을 쓰는 OEM 의 최근 영업이익?" |
| **CD-L4** | 시점 포함 공급망 ↔ 재무/ESG | 4 | 40~50% | "2023년 한온시스템 갱신한 OEM 중 ESG B+ 이상?" |

**CD-L1 우선 작성** — Bridge 매핑이 100% 검증된 4사 (현대차/현대모비스/현대위아/한국타이어) 만.

---

## 3. 큐레이션 워크플로

1. **DB 에서 정답 추출** — `psql` 또는 `cypher-shell` 로 직접 조회. LLM 으로 정답 생성 금지.
2. **paraphrase 3개 추가** — 한국어 수기 작성 (자동 생성 금지 — self-bias).
3. **엔티티 normalize** — `gold_answer_entities` 는 alias 까지 (예: `["현대자동차", "현대차"]`).
4. **`required_stores` / `main_hop_path` 채움** — 풀이 시 어느 저장소·노드 라벨이 필요한가.
5. **`is_answerable=false` 10% 포함** — DB 에 없는 사실 의도적 작성 (refusal 평가).

### lint 통과 필수

```bash
python scripts/audit/validate_gold_qa.py eval/qa_gold/*.jsonl
# 또는
make validate-gold-qa
```

lint 항목:
- 필수 필드 (`qid`, `question`, `question_type`, `complexity`, `domain`) 존재
- `qid` prefix 가 도메인/레벨과 일치 (`FIN-L1-*`, `AUTO-L2-*`, `CD-L3-*`)
- `evidence_corp_codes` 에 명시된 corp_code 가 `master.companies` 에 실재
- `gold_answer_text` 가 비어있고 `is_answerable=true` 면 경고 (정답 비어있는 정답행)
- Cross-Domain row 의 `domain` 은 반드시 `cross_domain`
- `complexity=hard` 면 `requires_multi_hop=true` 또는 `hop_count>=3` 권장

---

## 4. 자동 생성 / paraphrase 주의

- `gold_answer_text` 를 LLM 으로 자동 생성하면 self-bias (LLM 시스템 = LLM judge 같으면 점수 부풀려짐). 수기 작성 권장.
- 자동 생성한 row 는 `notes` 에 `"auto-filled"` 명시 + lint 가 경고만 출력.

---

## 5. 데이터 파일 매핑

| 파일 | 어디서 정답을 추출하나 |
|---|---|
| `gold_qa_v0.jsonl` (finance) | `fin.financials` (XBRL), `master.companies`, Neo4j SUBSIDIARY_OF / EXECUTIVE_OF / MAJOR_SHAREHOLDER_OF, `wiki.wikipedia_pages` |
| `gold_qa_auto_v0.jsonl` (auto) | `auto.master_*`, `auto.spec_measurements`, `auto.events_recalls`, `auto.events_complaints`, Neo4j Module / Supplier 그래프 |
| `gold_qa_cross_v0.jsonl` (cross) | `bridge.corp_entity` (4 매핑 사) 경유 — finance + auto 동시 |

---

## 6. 평가 실행

```bash
# 도메인 내 — 4 어댑터 × 100문항
make eval-full       # finance
make eval-auto       # auto

# Cross-Domain
make eval-cross      # → eval/reports/cross_<ts>/summary.md
```

각 평가는 `eval/reports/<run-id>/summary.md` + `manifest.json` 생성. PRD §10.6~§10.14
DoD 트래픽라이트는 `make audit-dod` 가 manifest 를 읽어 합산 리포트 생성.
