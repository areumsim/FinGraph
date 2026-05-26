"""DART 일괄 크롤러 — 회사별 (company.json + filings list + 재무 N년).

꼼꼼한 구현 요건:
- 진행률 표시 (tqdm)
- 이어받기 (이미 완료된 회사는 skip — manifest 기반)
- 실패 회복 (회사 1건 실패해도 계속, ledger 기록)
- Ctrl+C 안전 종료 (진행 중 회사 완료 후 멈춤)
- DART 일 한도 가드 (총 콜 수 카운터, --max-calls)
- 정합성: 회사당 산출 파일 형태 일관 (corp/{corp_code}/...)

사용:
    # 전체 (ingest_targets.jsonl 기준)
    python scripts/ingest/bulk_dart.py

    # 상위 50개만 시범
    python scripts/ingest/bulk_dart.py --limit 50

    # 이어받기 — 마지막 실행에서 실패한 것만
    python scripts/ingest/bulk_dart.py --retry-failed
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fingraph.config import get_settings  # noqa: E402
from fingraph.ingestion.dart_client import DartClient  # noqa: E402


# ── 산출 디렉토리 ──────────────────────────────────────────────────
def _corp_dir(root: Path, corp_code: str) -> Path:
    return root / "corp" / corp_code


def _company_path(root: Path, corp_code: str) -> Path:
    return _corp_dir(root, corp_code) / "company.json"


def _filings_path(root: Path, corp_code: str) -> Path:
    return _corp_dir(root, corp_code) / "filings.jsonl"


def _finstat_path(root: Path, corp_code: str, year: int) -> Path:
    return _corp_dir(root, corp_code) / "financials" / f"{year}_annual_CFS.jsonl"


def _manifest_path(root: Path) -> Path:
    return root / "_bulk_manifest.jsonl"


def _failed_ledger_path(root: Path) -> Path:
    return root / "_bulk_failed.jsonl"


# ── 진행 상태 ──────────────────────────────────────────────────────
@dataclass
class CorpStatus:
    corp_code: str
    company: bool = False
    filings: bool = False
    financials: dict[int, bool] = None  # year → ok

    def is_complete(self, years: list[int]) -> bool:
        return (
            self.company and self.filings
            and all((self.financials or {}).get(y, False) for y in years)
        )

    def to_dict(self) -> dict:
        return {
            "corp_code": self.corp_code,
            "company": self.company,
            "filings": self.filings,
            "financials": dict(self.financials or {}),
        }


def _load_manifest(root: Path) -> dict[str, CorpStatus]:
    """이전 실행의 manifest 로드 (이어받기)."""
    out: dict[str, CorpStatus] = {}
    p = _manifest_path(root)
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            cs = CorpStatus(
                corp_code=d["corp_code"],
                company=d.get("company", False),
                filings=d.get("filings", False),
                financials={int(k): bool(v) for k, v in (d.get("financials") or {}).items()},
            )
            out[cs.corp_code] = cs
    return out


def _append_manifest(root: Path, status: CorpStatus) -> None:
    with _manifest_path(root).open("a", encoding="utf-8") as f:
        f.write(json.dumps(status.to_dict(), ensure_ascii=False) + "\n")


def _append_failed(root: Path, corp_code: str, step: str, error: str) -> None:
    with _failed_ledger_path(root).open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "corp_code": corp_code, "step": step, "error": error,
            "ts": time.time(),
        }, ensure_ascii=False) + "\n")


# ── 회사별 수집 ────────────────────────────────────────────────────
def _fetch_company(
    client: DartClient, corp_code: str, root: Path, force: bool,
) -> bool:
    path = _company_path(root, corp_code)
    if path.exists() and not force:
        return True
    try:
        data = client.get_company_info(corp_code)
    except Exception as e:
        _append_failed(root, corp_code, "company", str(e))
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _fetch_filings(
    client: DartClient, corp_code: str, root: Path,
    bgn_de: str, end_de: str, force: bool,
) -> bool:
    path = _filings_path(root, corp_code)
    if path.exists() and not force:
        return True
    try:
        filings = list(client.iter_filings(
            corp_code=corp_code, bgn_de=bgn_de, end_de=end_de, pblntf_ty="A",
        ))
    except Exception as e:
        _append_failed(root, corp_code, "filings", str(e))
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for fl in filings:
            f.write(json.dumps(fl.__dict__, ensure_ascii=False) + "\n")
    return True


def _fetch_financials(
    client: DartClient, corp_code: str, root: Path, year: int, force: bool,
) -> bool:
    path = _finstat_path(root, corp_code, year)
    if path.exists() and not force:
        return True
    try:
        rows = client.get_single_finstat_all(
            corp_code=corp_code, bsns_year=str(year),
            reprt_code="11011", fs_div="CFS",
        )
    except Exception as e:
        _append_failed(root, corp_code, f"financials_{year}", str(e))
        return False
    if not rows:
        # 정상 응답이지만 데이터 없음 → 빈 파일로 표시 (다시 안 받게)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r.__dict__, ensure_ascii=False) + "\n")
    return True


# ── 메인 ──────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="DART 일괄 크롤러")
    parser.add_argument("--targets", type=Path, default=None,
                        help="ingest_targets.jsonl (기본: data/raw/ingest_targets.jsonl)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="data/raw/dart_bulk (기본)")
    parser.add_argument("--years", type=int, default=None,
                        help="최근 N년 (기본: settings.ingest_years_back)")
    parser.add_argument("--limit", type=int, default=None, help="상위 N 회사만")
    parser.add_argument("--force", action="store_true", help="이미 받은 것도 재수집")
    parser.add_argument("--retry-failed", action="store_true",
                        help="이전 실행 _bulk_failed.jsonl 의 회사만 재시도")
    parser.add_argument("--max-calls", type=int, default=8000,
                        help="DART API 호출 상한 (일 10,000 한도 가드, 기본 8,000)")
    parser.add_argument("--dry-run", action="store_true",
                        help="대상만 표시하고 실행 안 함")
    args = parser.parse_args()

    s = get_settings()
    targets_path = args.targets or (s.ingest_raw_dir / "ingest_targets.jsonl")
    out_dir = args.out_dir or (s.ingest_raw_dir / "dart_bulk")
    years_back = args.years or s.ingest_years_back

    today = date.today()
    bgn_de = f"{today.year - years_back}0101"
    end_de = today.strftime("%Y%m%d")
    years = list(range(today.year - years_back, today.year))

    if not targets_path.exists():
        print(f"[ERROR] targets 미발견: {targets_path}", file=sys.stderr)
        print("  먼저: python scripts/ingest/build_targets.py", file=sys.stderr)
        return 2
    if not s.dart_api_key:
        print("[ERROR] DART_API_KEY 미설정", file=sys.stderr)
        return 2

    # 대상 로드
    targets: list[dict] = []
    with targets_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                targets.append(json.loads(line))

    # retry-failed: 실패 ledger 의 corp_code 만 필터
    if args.retry_failed:
        failed_path = _failed_ledger_path(out_dir)
        if not failed_path.exists():
            print("[INFO] 실패 ledger 없음 — 재시도할 대상 없음")
            return 0
        failed_codes: set[str] = set()
        with failed_path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    failed_codes.add(json.loads(line)["corp_code"])
        targets = [t for t in targets if t["corp_code"] in failed_codes]
        # 재시도 시 ledger 초기화
        failed_path.unlink()
        print(f"[INFO] 실패 재시도: {len(targets)} 회사")

    if args.limit:
        targets = targets[:args.limit]

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] targets    : {len(targets)} 회사")
    print(f"[INFO] period     : {bgn_de} ~ {end_de} ({years} 사업연도)")
    print(f"[INFO] out_dir    : {out_dir}")
    print(f"[INFO] max_calls  : {args.max_calls}")

    if args.dry_run:
        print("[DRY] 실행 안 함")
        return 0

    # 이전 manifest 로드 → 이어받기
    prev = _load_manifest(out_dir)
    print(f"[INFO] 이전 진행  : {len(prev)} 회사 manifest 발견")

    # 우아한 종료
    stop_flag = {"stop": False}

    def _on_sigint(signum, frame):
        if stop_flag["stop"]:
            print("\n[ABORT] 두 번째 신호 — 즉시 종료", file=sys.stderr)
            sys.exit(130)
        print("\n[INFO] Ctrl+C 수신 — 현재 회사 완료 후 종료", file=sys.stderr)
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _on_sigint)

    # 진행률
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(it, **_):  # type: ignore[no-redef]
            return it

    call_count = 0
    summary = Counter()  # 'ok' / 'partial' / 'skip' / 'fail'

    with DartClient(api_key=s.dart_api_key, rate_limit_per_sec=s.ingest_rate_limit_per_sec) as client:
        for t in tqdm(targets, desc="ingest", unit="corp"):
            if stop_flag["stop"]:
                break
            corp_code = t["corp_code"]
            status = prev.get(corp_code, CorpStatus(corp_code=corp_code, financials={}))

            if status.is_complete(years) and not args.force:
                summary["skip"] += 1
                continue

            if call_count >= args.max_calls:
                print(f"\n[STOP] max_calls={args.max_calls} 도달", file=sys.stderr)
                break

            # 1. company
            if not status.company or args.force:
                if _fetch_company(client, corp_code, out_dir, args.force):
                    status.company = True
                call_count += 1

            # 2. filings
            if not status.filings or args.force:
                if _fetch_filings(client, corp_code, out_dir, bgn_de, end_de, args.force):
                    status.filings = True
                call_count += 1     # iter_filings 가 페이지 nav 하면 더 많음, 일단 1로 카운트

            # 3. financials (연도별)
            if status.financials is None:
                status.financials = {}
            for y in years:
                if status.financials.get(y, False) and not args.force:
                    continue
                if call_count >= args.max_calls:
                    break
                ok = _fetch_financials(client, corp_code, out_dir, y, args.force)
                status.financials[y] = ok
                call_count += 1

            # manifest 기록
            _append_manifest(out_dir, status)

            if status.is_complete(years):
                summary["ok"] += 1
            elif status.company or status.filings or any((status.financials or {}).values()):
                summary["partial"] += 1
            else:
                summary["fail"] += 1

    print()
    print("=" * 60)
    print(f"[DONE] API 호출 수 : {call_count:,}")
    print(f"[DONE] ok          : {summary['ok']:,}")
    print(f"[DONE] partial     : {summary['partial']:,}")
    print(f"[DONE] skip(이미)  : {summary['skip']:,}")
    print(f"[DONE] fail        : {summary['fail']:,}")
    print(f"[DONE] manifest    : {_manifest_path(out_dir)}")
    if _failed_ledger_path(out_dir).exists():
        print(f"[DONE] failed_ledger: {_failed_ledger_path(out_dir)} (재시도: --retry-failed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
