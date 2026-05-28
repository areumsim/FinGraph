#!/usr/bin/env python3
"""LAW.go.kr — 법령 메타데이터 일괄.

API: http://open.law.go.kr/LSO/openApi/  (무료 키)

본 시스템 범위:
- 금융 관련 법령 (자본시장법, 외부감사법, 공정거래법, 상법, 금융지주회사법, ...)
- 회사·산업과 LLM 으로 키워드 매칭 후 (:Industry)-[:REGULATED_BY]->(:Law) 적재
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from autonexusgraph.config import get_settings


KEY_LAWS_BY_NAME = [
    "자본시장과 금융투자업에 관한 법률",
    "주식회사 등의 외부감사에 관한 법률",
    "독점규제 및 공정거래에 관한 법률",
    "상법",
    "금융지주회사법",
    "은행법",
    "보험업법",
    "자산유동화에 관한 법률",
    "특정 금융거래정보의 보고 및 이용 등에 관한 법률",
    "근로기준법",
    "산업안전보건법",
    "전자상거래 등에서의 소비자보호에 관한 법률",
    "개인정보 보호법",
    "신용정보의 이용 및 보호에 관한 법률",
    "공정거래법",
]


def main() -> int:
    s = get_settings()
    if not s.law_api_key:
        print("LAW_API_KEY 미설정 — open.law.go.kr/LSO/openApi 에서 무료 키 발급 후 .env 추가")
        print("우선 스크립트 + 적재 코드 준비됨. 키 확보 후 동일 명령 재실행.")
        print(f"\n[정의된 관심 법령] {len(KEY_LAWS_BY_NAME)} 개:")
        for n in KEY_LAWS_BY_NAME:
            print(f"  - {n}")
        return 1

    # TODO: LAW API 호출 — 키 확보 후 endpoint 검증 필요
    print("[law] LAW_API_KEY 확인됨. 실제 다운로드 로직은 endpoint 검증 후 활성화.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
