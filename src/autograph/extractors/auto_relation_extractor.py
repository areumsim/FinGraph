"""AutoRelationExtractor — autonexusgraph.extractors.base.BaseExtractor 자동차 도메인 구현.

설계:
- finance 측의 ExtractorEngine / RunContext / safe_extract 기반시설을 그대로 활용.
- 프롬프트는 ``prompts/relation_extract_auto.yaml`` SSOT.
- 출력은 ExtractorResult.relations — 각 rel dict 에 ``head_kind`` / ``tail_kind`` 가
  있어 staging_writer 가 (head_text_norm, tail_text_norm) merge key 를 만들 수 있다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from autonexusgraph.extractors.base import (
    BaseExtractor,
    ExtractorResult,
    RunContext,
)


log = logging.getLogger(__name__)


_PROMPT_PATH = Path(__file__).parent / "prompts" / "relation_extract_auto.yaml"


def load_auto_prompt() -> dict[str, Any]:
    return yaml.safe_load(_PROMPT_PATH.read_text(encoding="utf-8"))


class AutoRelationExtractor(BaseExtractor):
    """vec.chunks (자동차) → SUPPLIED_BY / RECALL_OF 관계 후보."""

    name = "auto_relation_extractor"
    version = "p3-auto-v1"
    timeout_ms = 60_000
    deterministic = False           # LLM 사용

    def __init__(self, *, purpose: str = "auto_p3") -> None:
        self.purpose = purpose
        self.prompt = load_auto_prompt()

    def healthcheck(self) -> bool:
        return bool(self.prompt and self.prompt.get("system") and self.prompt.get("user_template"))

    def extract(self, chunk: dict, ctx: RunContext) -> ExtractorResult:
        client = ctx.llm_client
        if client is None:
            return ExtractorResult.empty(self.name, self.version,
                warnings=("no_llm_client",))

        # ctx.extra 에 manufacturer name resolver 가 들어있다고 가정.
        name_resolver: dict[int, str] = ctx.extra.get("manufacturer_names", {})
        mfr_id = chunk.get("manufacturer_id")
        mfr_name = name_resolver.get(mfr_id, "") if mfr_id else ""

        text = (chunk.get("text") or "")[:4000]    # safety cut
        user = self.prompt["user_template"].format(
            manufacturer_id=mfr_id or "",
            manufacturer_name=mfr_name,
            model_id=chunk.get("model_id") or "",
            model_name=(chunk.get("metadata") or {}).get("model_name", ""),
            variant_id=chunk.get("variant_id") or "",
            snapshot_year=(chunk.get("metadata") or {}).get("snapshot_year")
                          or chunk.get("snapshot_year") or "",
            source=chunk.get("source") or "",
            section=chunk.get("section") or "",
            chunk_id=chunk.get("id"),
            chunk_text=text,
        )

        messages = [
            {"role": "system", "content": self.prompt["system"]},
            {"role": "user",   "content": user},
        ]
        try:
            out = client.chat_json(messages, schema=self.prompt["json_schema"],
                                    temperature=0.0, purpose=self.purpose)
        except Exception as e:  # noqa: BLE001 — engine 의 safe_extract 가 자세히 wrap.
            raise

        rels = out.get("relations") or []
        # 다운스트림 (staging_writer / engine.merge) 가 사용하는 메타 보강.
        for r in rels:
            r["_extracted_by"] = self.name
            r["_chunk_id"] = chunk.get("id")
            r["_manufacturer_id"] = mfr_id
            r["_model_id"] = chunk.get("model_id")
            r["_variant_id"] = chunk.get("variant_id")
            r["_source"] = chunk.get("source")
            r["_snapshot_year"] = (
                (chunk.get("metadata") or {}).get("snapshot_year")
                or chunk.get("snapshot_year")
            )
        return ExtractorResult(
            relations=tuple(rels),
            extractor_name=self.name,
            extractor_version=self.version,
        )


__all__ = ["AutoRelationExtractor", "load_auto_prompt"]
