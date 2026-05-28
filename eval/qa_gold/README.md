# qa_gold — 평가용 정답 데이터셋

본 디렉토리는 **데이터·도메인 시나리오는 비어있음**. 패턴(스키마·필드)만 정의.
AutoNexusGraph 도메인(한국 상장사·금융) 에 맞는 100문항 QA 를 직접 큐레이션해 채워야 함.

## 스키마 (1 row = 1 line jsonl)

```json
{
  "qid":                 "Q0001",                 // 고유 ID
  "question":            "삼성전자 2024년 매출은?",
  "question_type":       "single_entity",         // single_entity | multi_entity | relation | aggregation | ranking | comparison
  "complexity":          "easy",                  // easy | medium | hard
  "requires_multi_hop":  false,                   // PRD §2.2 multi-hop 75%+ 측정용

  "gold_answer_text":    ["300조 8천억원", "약 300조원"],   // paraphrase 허용. EM/F1 의 max 매칭
  "gold_answer_entities": ["삼성전자(주)", "매출"],         // Hits@k 매칭용

  "evidence_doc_ids":    ["rcept_no_or_chunk_ids"],         // 선택 — Recall@k 측정용
  "evidence_corp_codes": ["00126380"],

  "gold_cypher":         null,                    // 선택 — execution_accuracy 측정용

  "scenario_id":         null,                    // 선택 — 시나리오별 집계용. 사용자 정의.

  "is_answerable":       true,                    // false 면 refusal precision/recall 측정에 사용
  "notes":               ""
}
```

## 큐레이션 가이드

PRD §3.3 의 100문항 (L1 30 / L2 40 / L3 30) 구성 권장:

- **L1 (factual, easy)**: "삼성전자 2024년 매출은?" — `tools.financials.get_revenue` 단일 호출
- **L2 (2-hop, medium)**: "삼성전자 자회사 중 매출 1조 이상은?" — graph + financials
- **L3 (3-hop+, hard)**: "이재용이 임원인 회사들의 합산 영업이익은?" — graph + financials + multi-step

균형:
- 30%: factual (매출/영업이익/순이익/자산 — 정확값)
- 40%: structural (자회사/임원/주주/그룹 관계)
- 30%: narrative (위험요인/사업개요 — 의미 검색)

## 위험 — paraphrase / self-bias 주의

- `gold_answer_text` 를 LLM 으로 자동 생성하면 self-bias (LLM 시스템 = LLM judge 같으면 점수 부풀려짐). 수기 작성 권장.
- 자동 생성한 row 는 `notes` 에 "auto-filled" 명시.

## 데이터 파일

- `gold_qa_v0.jsonl` : 정식 평가셋 (사용자가 큐레이션)
- `gold_qa_v0.example.jsonl` : 3 row 예시 (이 패키지 동작 검증용)
