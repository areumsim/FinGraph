"""P3 — 사업보고서 본문 청크 → LLM 관계 추출.

처리 흐름:
1. vec.chunks 에서 high-value section + 대상 회사 필터로 청크 가져오기
2. 청크별 컨텍스트 (회사명/연도/섹션) + LLMClient.chat_json 호출
3. 출력 JSON 검증 (relations.yaml 의 P3 관계만 허용) + confidence gate
4. 결과 → data/processed/extracted/<corp_code>/<rcept_no>.jsonl 저장
   ( 별도 적재 스크립트가 Neo4j 로 보냄 — P4 검증 후 )

비용 가드 (memory: feedback-llm-cost-brake):
- 호출 전 estimate → BudgetCheck (--dry-run / --approve-cost)
- 런타임: budget_aware_client wrapper 가 자동 record/guard

설계:
- LLM 호출 부분은 어댑터 (LLMClient) 만 알고 provider 무관.
- 프롬프트는 prompts/relation_extract.yaml SSOT 에서 로드.
- 멱등: processed/extracted/.../<rcept_no>.jsonl 존재하면 skip (force 시 덮어쓰기).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import get_settings
from ..llm.base import get_llm_client
from ..llm.budget_aware import budget_aware_client


log = logging.getLogger(__name__)


# vec.chunks 에서 P3 추출에 의미 있는 section keyword. 너무 길면 노이즈, 너무 짧으면 의미 없음.
HIGH_VALUE_SECTIONS = (
    "사업의 개요",
    "사업의개요",
    "주요거래처",
    "주요 거래처",
    "위험요인",
    "위험 요인",
    "주주",
    "지배구조",
)


@dataclass
class P3Result:
    """추출 1건 — 입력 청크 메타 + LLM 산출."""
    chunk_id: int
    corp_code: str
    rcept_no: str | None
    fiscal_year: int | None
    section: str
    entities: list[dict]
    relations: list[dict]
    raw_json: dict


def load_prompt(path: Path | None = None) -> dict[str, Any]:
    """prompts/relation_extract.yaml 로드 (SSOT)."""
    p = path or (Path(__file__).resolve().parents[1] / "prompts" / "relation_extract.yaml")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def filter_target_chunks(
    *,
    corp_codes: list[str],
    fiscal_years: list[int] | None = None,
    sections_contain: tuple[str, ...] = HIGH_VALUE_SECTIONS,
    min_token_count: int = 80,
    max_token_count: int = 1200,
    limit_per_corp_year: int = 5,
    apply_selectivity: bool = True,
    selectivity_min_signal: int = 2,
    selectivity_min_corp_candidates: int = 2,
) -> list[dict]:
    """대상 청크 SELECT — 비용 가드의 첫 라인 (2단 필터).

    1단 (SQL): section / token_count / per-corp-year cap (호출 수 폭증 방지).
    2단 (Python — apply_selectivity=True): signal-based filter.
       회사명 후보 ≥ 2 + 금융 signal ≥ 2 인 청크만 통과. 호출 수 50%+ 추가 절감.
    """
    from ..db.postgres import get_pool

    section_clause = " OR ".join(["section ILIKE %s"] * len(sections_contain))
    params: list[Any] = [f"%{s}%" for s in sections_contain]
    where = f"corp_code = ANY(%s) AND ({section_clause})"
    params = [corp_codes] + params + [min_token_count, max_token_count]
    where += " AND token_count BETWEEN %s AND %s"

    if fiscal_years:
        where += " AND fiscal_year = ANY(%s)"
        params.append(fiscal_years)

    # 회사·연도 조합당 N 개 — window function 사용
    sql = f"""
    WITH ranked AS (
      SELECT id, corp_code, rcept_no, fiscal_year, section, text, token_count,
             row_number() OVER (PARTITION BY corp_code, fiscal_year, section
                                ORDER BY token_count DESC) AS rk
        FROM vec.chunks
       WHERE {where}
    )
    SELECT id, corp_code, rcept_no, fiscal_year, section, text, token_count
      FROM ranked
     WHERE rk <= %s
     ORDER BY corp_code, fiscal_year DESC, section
    """
    params.append(limit_per_corp_year)

    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    # 2단 — signal-based selectivity (Python). LLM 호출 전 추가 절감.
    if apply_selectivity and rows:
        from ._selective import filter_chunks_by_selectivity
        kept, skipped = filter_chunks_by_selectivity(
            rows,
            min_signal_count=selectivity_min_signal,
            min_corp_candidates=selectivity_min_corp_candidates,
        )
        log.info(f"[p3.filter] sql={len(rows)} → selective_kept={len(kept)} "
                 f"(skipped={len(skipped)}, savings={100*len(skipped)/max(len(rows),1):.1f}%)")
        return kept
    return rows


def estimate_p3_cost(chunks: list[dict], model: str = "gpt-4o-mini") -> Any:
    """선택된 청크 묶음으로 P3 LLM 호출 비용 추정 — 진입점에서 dry-run 출력용."""
    from ..llm.cost import estimate

    n_calls = len(chunks)
    # 프롬프트 system + user_template + chunk_text 합산. 한국어 ≈ 1 tok / 3 chars.
    avg_chunk_chars = sum(len(c["text"]) for c in chunks) / max(1, n_calls)
    # system ~600 + user template ~300 + chunk_chars/3
    avg_in_tokens = 900 + avg_chunk_chars / 3
    avg_out_tokens = 300                  # 보통 JSON 응답 짧음
    return estimate(model, n_calls, avg_in_tokens, avg_out_tokens)


def extract_one(
    chunk: dict,
    *,
    company_name_resolver: dict[str, str] | None = None,
    client: Any | None = None,
    prompt: dict[str, Any] | None = None,
    model_role: str = "research",
    purpose: str = "p3_extract",
) -> P3Result | None:
    """단일 청크 → P3Result. LLM 호출 실패 시 None.

    company_name_resolver: corp_code → 회사명 매핑 (PG master.companies 에서 prefetch).
    client: BudgetAwareLLMClient — 미지정 시 새로 만들지 않음 (호출자가 manage).
    """
    if client is None:
        raise ValueError("client (BudgetAwareLLMClient) 필수")
    prompt = prompt or load_prompt()
    name = (company_name_resolver or {}).get(chunk["corp_code"], chunk["corp_code"])

    user_text = prompt["user_template"].format(
        company_name=name,
        corp_code=chunk["corp_code"],
        fiscal_year=chunk.get("fiscal_year") or "-",
        section=chunk.get("section") or "",
        chunk_text=chunk["text"][:4000],   # safety cut
    )
    messages = [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": user_text},
    ]
    try:
        out = client.chat_json(messages, schema=prompt["json_schema"],
                                temperature=0.0, purpose=purpose)
    except Exception as e:
        log.warning(f"[p3] chunk {chunk['id']} LLM failed: {e}")
        return None

    return P3Result(
        chunk_id=chunk["id"],
        corp_code=chunk["corp_code"],
        rcept_no=chunk.get("rcept_no"),
        fiscal_year=chunk.get("fiscal_year"),
        section=chunk.get("section") or "",
        entities=out.get("entities") or [],
        relations=out.get("relations") or [],
        raw_json=out,
    )


def save_result(result: P3Result, root: Path | None = None) -> Path:
    """processed/extracted/<corp_code>/<rcept_no_or_chunk>.jsonl 에 한 줄 append."""
    settings = get_settings()
    root = root or (settings.ingest_processed_dir / "extracted")
    rcept = result.rcept_no or f"chunk_{result.chunk_id}"
    out_dir = root / result.corp_code
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{rcept}.jsonl"
    line = {
        "chunk_id": result.chunk_id,
        "corp_code": result.corp_code,
        "rcept_no": result.rcept_no,
        "fiscal_year": result.fiscal_year,
        "section": result.section,
        "entities": result.entities,
        "relations": result.relations,
    }
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return out_path


__all__ = [
    "HIGH_VALUE_SECTIONS",
    "P3Result",
    "load_prompt",
    "filter_target_chunks",
    "estimate_p3_cost",
    "extract_one",
    "save_result",
]
