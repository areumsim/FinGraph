"""number_guard — 화이트리스트 / evidence 라벨링 / prompt 형식."""

from __future__ import annotations

from fingraph.agents.number_guard import (
    _BIG_NUMBER_RE,
    collect_approved_numbers,
    format_approved_for_prompt,
    sanitize_evidence_for_synth,
)


# ── collect_approved_numbers ────────────────────────────────
def test_collect_from_tool_results():
    state = {
        "tool_results": [
            {"result": {"value": "258,935,500,000,000"}},
            {"result": "영업이익 12,345,678,000"},
        ],
        "evidence_chunks": [],
    }
    nums = collect_approved_numbers(state)
    assert "258935500000000" in nums
    assert "12345678000" in nums


def test_collect_from_evidence_chunks():
    state = {
        "tool_results": [],
        "evidence_chunks": [
            {"text": "매출은 258,935,500,000,000원으로 증가했다."},
        ],
    }
    assert "258935500000000" in collect_approved_numbers(state)


def test_collect_ignores_small_and_identifiers():
    state = {
        "tool_results": [{"result": "2023년 보고서, corp=00126380, ratio 9.5%"}],
        "evidence_chunks": [{"text": "비율 12.3% 였다."}],
    }
    nums = collect_approved_numbers(state)
    # 2023 (4자리 연도), 00126380 (leading-0), 12.3 (소수) 모두 제외
    assert nums == set()


def test_collect_combines_tool_and_evidence():
    state = {
        "tool_results": [{"result": "1,234,567,890"}],
        "evidence_chunks": [{"text": "9,876,543,210원"}],
    }
    nums = collect_approved_numbers(state)
    assert "1234567890" in nums
    assert "9876543210" in nums


# ── sanitize_evidence_for_synth ─────────────────────────────
def test_sanitize_marks_approved_numbers():
    approved = {"258935500000000"}
    out = sanitize_evidence_for_synth(
        [{"text": "매출 258,935,500,000,000원 기록"}],
        approved,
    )
    assert "[수치:258,935,500,000,000]" in out[0]["text"]


def test_sanitize_replaces_unapproved_with_warning_label():
    approved = {"258935500000000"}
    out = sanitize_evidence_for_synth(
        [{"text": "출처 불명 수치 999,999,999,999 가 있다"}],
        approved,
    )
    assert "[검증불가:999,999,999,999]" in out[0]["text"]


def test_sanitize_does_not_mutate_original():
    chunks = [{"text": "1,234,567,890 원"}]
    sanitize_evidence_for_synth(chunks, set())
    # 원본 그대로
    assert chunks[0]["text"] == "1,234,567,890 원"


def test_sanitize_respects_cap():
    approved: set[str] = set()
    chunks = [{"text": f"chunk {i}"} for i in range(20)]
    out = sanitize_evidence_for_synth(chunks, approved, cap=3)
    assert len(out) == 3


def test_sanitize_truncates_text():
    approved: set[str] = set()
    long = "x" * 1000
    out = sanitize_evidence_for_synth([{"text": long}], approved, text_max=100)
    assert len(out[0]["text"]) <= 100


def test_sanitize_handles_missing_text():
    approved: set[str] = set()
    out = sanitize_evidence_for_synth([{"corp_code": "00126380"}], approved)
    assert out[0]["text"] == ""


# ── format_approved_for_prompt ──────────────────────────────
def test_format_empty_set_returns_no_numbers_note():
    s = format_approved_for_prompt(set())
    assert "없음" in s or "금지" in s


def test_format_short_list_full():
    approved = {"1234567890", "9876543210"}
    s = format_approved_for_prompt(approved)
    # 천 단위 콤마 형식
    assert "1,234,567,890" in s
    assert "9,876,543,210" in s


def test_format_caps_at_limit():
    approved = {str(10**i + j) for i in range(7, 9) for j in range(20)}
    s = format_approved_for_prompt(approved, limit=5)
    assert "외" in s and "개" in s


def test_format_uses_commas_for_pure_int():
    s = format_approved_for_prompt({"1000000"})
    assert "1,000,000" in s


# ── 정규식 자체 ─────────────────────────────────────────────
def test_regex_picks_only_big_numbers():
    nums = [m.group(0) for m in _BIG_NUMBER_RE.finditer(
        "ROE 9.5%, 매출 258,935,500,000,000원, corp=00126380, 9876543210원"
    )]
    # 콤마 그룹 ≥ 2 → 258,935,500,000,000 ✓
    # leading 1-9 + 7+자리 → 9876543210 ✓
    # 9.5 (소수), 00126380 (leading 0) → 제외
    assert "258,935,500,000,000" in nums
    assert "9876543210" in nums
    assert all("00126380" not in n for n in nums)
