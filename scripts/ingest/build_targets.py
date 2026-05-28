"""적재 대상 목록 작성 — DART corp_code ↔ KRX stock_code 매칭.

입력:
    data/raw/dart/corp_codes_listed.csv     (DART)
    data/raw/krx/top_kospi_200.csv          (KRX top 200)
    data/raw/krx/top_kosdaq_100.csv         (KRX top 100)

출력:
    data/raw/ingest_targets.jsonl
        {corp_code, stock_code, name_dart, name_krx, market, market_cap}

매칭 키: stock_code (6자리).
DART corp_codes_listed 에는 stock_code 가 있으므로 단순 inner-join.

사용:
    python scripts/ingest/build_targets.py
    python scripts/ingest/build_targets.py --markets KOSPI  # KOSDAQ 제외
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from autonexusgraph.config import get_settings  # noqa: E402


def _load_dart_listed(path: Path) -> dict[str, dict]:
    """stock_code → dart row 매핑."""
    out: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sc = row.get("stock_code", "").strip().zfill(6)
            if sc and sc != "000000":
                out[sc] = row
    return out


def _load_krx_top(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="DART × KRX 적재 대상 매칭")
    parser.add_argument("--dart-csv", type=Path, default=None,
                        help="DART corp_codes_listed.csv")
    parser.add_argument("--krx-dir", type=Path, default=None,
                        help="KRX top_*.csv 디렉토리")
    parser.add_argument("--markets", type=str, default="KOSPI,KOSDAQ",
                        help="대상 시장 (쉼표 구분)")
    parser.add_argument("--out", type=Path, default=None,
                        help="출력 (기본: data/raw/ingest_targets.jsonl)")
    args = parser.parse_args()

    s = get_settings()
    dart_csv = args.dart_csv or (s.ingest_raw_dir / "dart" / "corp_codes_listed.csv")
    krx_dir = args.krx_dir or (s.ingest_raw_dir / "krx")
    out_path = args.out or (s.ingest_raw_dir / "ingest_targets.jsonl")

    if not dart_csv.exists():
        print(f"[ERROR] DART corp_codes 미발견: {dart_csv}", file=sys.stderr)
        print("        먼저 `python scripts/ingest/download_corp_codes.py`", file=sys.stderr)
        return 2

    dart_map = _load_dart_listed(dart_csv)
    print(f"[INFO] DART 상장사 {len(dart_map):,}건 로드")

    markets = [m.strip().upper() for m in args.markets.split(",")]

    krx_targets: list[dict] = []
    for mkt in markets:
        n_default = 200 if mkt == "KOSPI" else 100 if mkt == "KOSDAQ" else 50
        path = krx_dir / f"top_{mkt.lower()}_{n_default}.csv"
        if not path.exists():
            print(f"[WARN] {path} 없음 — `make ingest-krx` 먼저 실행", file=sys.stderr)
            continue
        rows = _load_krx_top(path)
        krx_targets.extend(rows)
        print(f"[INFO] KRX {mkt} {len(rows):,}건 로드")

    # 매칭
    matched: list[dict] = []
    unmatched: list[dict] = []
    for krx_row in krx_targets:
        sc = krx_row["stock_code"].strip().zfill(6)
        dart_row = dart_map.get(sc)
        if dart_row:
            matched.append({
                "corp_code":  dart_row["corp_code"],
                "stock_code": sc,
                "name_dart":  dart_row["corp_name"],
                "name_krx":   krx_row["name"],
                "market":     krx_row["market"],
                "market_cap": int(krx_row["market_cap"]) if krx_row.get("market_cap") else None,
                "sector":     krx_row.get("sector") or None,
                "isin":       krx_row.get("isin") or None,
            })
        else:
            unmatched.append({"stock_code": sc, "name_krx": krx_row["name"]})

    # 출력
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for t in matched:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    print()
    print(f"[OK] matched   : {len(matched):,} → {out_path}")
    print(f"[!! ] unmatched: {len(unmatched):,}")
    if unmatched:
        print("     (앞 10개):")
        for u in unmatched[:10]:
            print(f"       - {u['stock_code']}  {u['name_krx']}")
        unmatched_path = out_path.with_name("ingest_targets_unmatched.jsonl")
        with unmatched_path.open("w", encoding="utf-8") as f:
            for u in unmatched:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
        print(f"     → {unmatched_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
