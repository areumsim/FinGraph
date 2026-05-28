"""AutoGraph 자동차 도메인 온톨로지 로더 (SSOT).

ontology/auto/{entities,relations,extractors,system_taxonomy,standards,plants}.yaml
을 한 곳에서 로드. 다음 모듈이 본 로더를 통해 SSOT 에 접근한다:

- ``loaders.neo4j_init``  : 라벨 + key 컬럼 → CONSTRAINT 자동 생성
- ``loaders.load_*``       : 엣지 적재 시 §6.7 의무 메타 키 강제
- ``extractors/*``         : 프롬프트에 entity/relation 표 주입 (schema-aware)
- ``extractors.cross_validate``: 관계 from/to 라벨 검증 + confidence_default

주의: 본 로더는 ``autonexusgraph/`` (금융) 의 ``ontology/*.yaml`` 은 건드리지 않음.
finance 측은 자체 코드에서 직접 ``ontology/`` 를 읽는다 (변경 없음).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


# repo_root/ontology/auto/
_ONTOLOGY_DIR = Path(__file__).resolve().parents[2] / "ontology" / "auto"


@lru_cache(maxsize=1)
def load_entities() -> dict[str, dict[str, Any]]:
    """entities.yaml → {label: spec}."""
    data = yaml.safe_load((_ONTOLOGY_DIR / "entities.yaml").read_text(encoding="utf-8"))
    return data["entities"]


@lru_cache(maxsize=1)
def load_relations() -> dict[str, dict[str, Any]]:
    """relations.yaml → {rel_type: spec}. 'edge_required_meta' 는 별도 함수."""
    data = yaml.safe_load((_ONTOLOGY_DIR / "relations.yaml").read_text(encoding="utf-8"))
    return data["relations"]


@lru_cache(maxsize=1)
def load_edge_required_meta() -> tuple[str, ...]:
    """relations.yaml::edge_required_meta — 모든 엣지가 가져야 할 속성 키."""
    data = yaml.safe_load((_ONTOLOGY_DIR / "relations.yaml").read_text(encoding="utf-8"))
    return tuple(data.get("edge_required_meta", ()))


@lru_cache(maxsize=1)
def load_extractors() -> dict[str, dict[str, Any]]:
    """extractors.yaml → {extractor_name: spec}."""
    data = yaml.safe_load((_ONTOLOGY_DIR / "extractors.yaml").read_text(encoding="utf-8"))
    return data["extractors"]


@lru_cache(maxsize=1)
def load_system_taxonomy() -> dict[str, dict[str, Any]]:
    """system_taxonomy.yaml → {code: {name, description, alias_codes}}.

    alias_codes 는 AI-Hub 로더가 'powertrain' 같은 raw code 를 canonical 'POWERTRAIN'
    으로 정규화할 때 참조.
    """
    data = yaml.safe_load((_ONTOLOGY_DIR / "system_taxonomy.yaml").read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for row in data["systems"]:
        out[row["code"]] = row
    return out


@lru_cache(maxsize=1)
def load_standards() -> list[dict[str, Any]]:
    """standards.yaml → [Standard rows]."""
    data = yaml.safe_load((_ONTOLOGY_DIR / "standards.yaml").read_text(encoding="utf-8"))
    return data["standards"]


@lru_cache(maxsize=1)
def load_plants() -> list[dict[str, Any]]:
    """plants.yaml → [Plant rows]."""
    data = yaml.safe_load((_ONTOLOGY_DIR / "plants.yaml").read_text(encoding="utf-8"))
    return data["plants"]


@lru_cache(maxsize=1)
def load_manufactured_at_seed() -> list[dict[str, Any]]:
    """manufactured_at_seed.yaml → [(model_name, manufacturer, plant_code, valid_from)]."""
    p = _ONTOLOGY_DIR / "manufactured_at_seed.yaml"
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data.get("mappings") or []


@lru_cache(maxsize=1)
def _alias_to_canonical_system() -> dict[str, str]:
    """raw system code (대소문자, alias) → canonical SCREAMING_SNAKE_CASE.

    AI-Hub / 매뉴얼 / LLM 산출에 등장하는 다양한 표기를 단일 코드로 모은다.
    """
    out: dict[str, str] = {}
    for code, row in load_system_taxonomy().items():
        out[code.upper()] = code
        out[code.lower()] = code
        for alias in row.get("alias_codes") or []:
            if alias:
                out[alias.upper()] = code
                out[alias.lower()] = code
    return out


def canonical_system_code(raw: str | None) -> str:
    """'powertrain' / 'ENGINE' / 'powertrain ' → 'POWERTRAIN'.

    매칭 실패 시 'UNKNOWN' 반환 (none/빈문자도 동일).
    """
    if not raw:
        return "UNKNOWN"
    key = raw.strip()
    if not key:
        return "UNKNOWN"
    table = _alias_to_canonical_system()
    return table.get(key, table.get(key.upper(), table.get(key.lower(), "UNKNOWN")))


def entity_key_property(label: str) -> str:
    """라벨의 자연 키 속성명. neo4j_init 가 CONSTRAINT 만들 때 사용."""
    spec = load_entities().get(label)
    if not spec:
        raise KeyError(f"unknown entity label: {label}")
    return spec.get("key", "id")


def entity_labels() -> list[str]:
    """ontology 가 정의한 자동차 도메인의 모든 라벨."""
    return list(load_entities().keys())


def relation_types() -> list[str]:
    """ontology 가 정의한 모든 관계 타입."""
    return list(load_relations().keys())


def relation_endpoints(rel_type: str) -> tuple[str, str]:
    """관계 from→to 라벨. cross_validate / prompt 에서 사용."""
    spec = load_relations()[rel_type]
    return spec["from"], spec["to"]


def render_entity_table_for_prompt() -> str:
    """LLM 프롬프트에 주입할 entity 타입 표 (markdown).

    relation_extract_auto.yaml 의 ``{entity_types_table}`` 자리에 들어간다.
    """
    lines = ["| 라벨 | 설명 | 키 |", "|---|---|---|"]
    for label, spec in load_entities().items():
        desc = (spec.get("description") or "").strip().splitlines()[0]
        lines.append(f"| {label} | {desc} | {spec.get('key', 'id')} |")
    return "\n".join(lines)


def render_relation_table_for_prompt(*, enabled_only: bool = True) -> str:
    """LLM 프롬프트의 ``{relation_types_table}`` 자리에 들어가는 표."""
    lines = ["| 관계 | From | To | 신뢰도 기본 | 비고 |",
             "|---|---|---|---|---|"]
    for rt, spec in load_relations().items():
        if enabled_only and not spec.get("enabled", True):
            continue
        note_bits = []
        if spec.get("class"):
            note_bits.append(spec["class"])
        if spec.get("provenance"):
            note_bits.append(spec["provenance"])
        lines.append(
            f"| {rt} | {spec['from']} | {spec['to']} | "
            f"{spec.get('confidence_default', 0.7):.2f} | {', '.join(note_bits)} |"
        )
    return "\n".join(lines)


__all__ = [
    "load_entities",
    "load_relations",
    "load_edge_required_meta",
    "load_extractors",
    "load_system_taxonomy",
    "load_standards",
    "load_plants",
    "canonical_system_code",
    "entity_key_property",
    "entity_labels",
    "relation_types",
    "relation_endpoints",
    "render_entity_table_for_prompt",
    "render_relation_table_for_prompt",
]
