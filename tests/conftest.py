"""Pytest fixtures — 공통 격리 처리.

LangGraph 가 환경에 설치돼 있으면 ``_run_with_langgraph`` 경로로 분기하는데,
컴파일 시점에 노드 함수 참조가 StateGraph 에 캡쳐되어 ``unittest.mock.patch``
가 더 이상 노드에 도달하지 못한다. 이 unit test 모음은 폴백 체인 동작을
검증하는 목적이므로 graph/stream 관련 테스트 자동으로 ``_HAS_LANGGRAPH=False``
로 강제한다. LangGraph StateGraph 자체의 round-trip 은 별도 integration
테스트로 분리 (`tests -m integration`).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _force_fallback_chain_for_graph_tests(request, monkeypatch):
    """graph/stream/smoke 테스트는 폴백 체인 강제.

    예외:
    1. ``test_runtime_branch_is_either_langgraph_or_fallback`` — 환경 분기 자체 검증.
    2. ``@pytest.mark.integration`` 마커 — 실제 LangGraph round-trip 검증 의도.
    """
    mod = request.module.__name__
    if not any(key in mod for key in ("test_graph_smoke", "test_stream")):
        return
    if request.node.name == "test_runtime_branch_is_either_langgraph_or_fallback":
        return
    if request.node.get_closest_marker("integration"):
        return   # integration 마커 — 실제 LangGraph 검증.
    import autonexusgraph.agents.graph as g
    monkeypatch.setattr(g, "_HAS_LANGGRAPH", False)
    monkeypatch.setattr(g, "_LG_APP", None)
