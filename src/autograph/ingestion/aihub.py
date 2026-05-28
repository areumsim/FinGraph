"""AI Hub (aihub.or.kr) 데이터셋 다운로드 wrapper.

NIA 한국지능정보사회진흥원 의 AI 학습 데이터 — 자동차 도메인에 유용한 데이터셋:

| datasetkey | 이름 | 용량 | AutoGraph 활용 |
|-----------|------|------|----------------|
| 71347      | 자율주행 고장진단 데이터 | 1.31 TB | motor-reducer / battery 결함 라벨 |
| 578        | 부품 품질 검사 영상 데이터(자동차) | 452.81 GB | 자동차 부품 11종 결함 분류 |

**중요**: 원본 데이터 (TS*.tar, VS*.zip) 는 수십~수백 GB 의 이미지/시계열 — AutoGraph
(텍스트 RAG) 에는 적합하지 않음. **라벨링 데이터 (TL*.tar / VL*.zip — 수백 MB)** 만으로
도 충분히 part taxonomy + defect classification 추출 가능. 기본값 ``--labels-only``.

사전 요건:
1. https://aihub.or.kr 회원가입 후 마이페이지 → API key 발급 (UUID, 이메일 수신)
   → .env 의 ``AIHUB_API_KEY`` 에 저장.
2. 다운로드 대상 데이터셋 상세 페이지 → "다운로드" 버튼 클릭 → 승인 완료.
3. bin/aihubshell 가 다운로드돼 있어야 함 (저장소 동봉; chmod +x 됐는지 확인).

CLI:
    python -m autograph.ingestion.aihub --dataset 578 --labels-only
    python -m autograph.ingestion.aihub --dataset 71347 --labels-only
    python -m autograph.ingestion.aihub --dataset 578 --filekeys 57231,57232  # 특정 파일만
    python -m autograph.ingestion.aihub --dataset 578 --list   # 파일 목록만 조회
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from pathlib import Path

from autonexusgraph.config import get_settings
from autonexusgraph.ingestion._common import CheckpointStore
from ..config import get_auto_settings


log = logging.getLogger(__name__)


_SOURCE = "auto/aihub"

# 라벨 키 매핑 — 원본은 제외, 라벨링 데이터 (TL*/VL*) 만 화이트리스트.
# 데이터셋 페이지 표기 기반. 신규 데이터셋 추가 시 여기 보강.
_LABEL_FILEKEYS: dict[int, list[int]] = {
    # 71347 자율주행 고장진단 — TL.zip (461 MB) + VL.zip (58 MB)
    71347: [495026, 495029],
    # 578 부품 품질 검사 영상 자동차 — TL_*.tar 11종 + VL_*.tar 11종 = ~700 MB
    578: [
        57231, 57232, 57233, 57234, 57235, 57236, 57237, 57238, 57239, 57240, 57241,  # Training TL
        57254, 57255, 57256, 57257, 57258, 57259, 57260, 57261, 57262, 57263, 57264,  # Validation VL
    ],
}


def _aihubshell_path() -> Path:
    """프로젝트 동봉된 aihubshell 위치. 없으면 에러."""
    root = Path(__file__).resolve().parents[3]
    p = root / "bin" / "aihubshell"
    if not p.exists():
        raise FileNotFoundError(
            f"aihubshell 없음: {p}. "
            "curl -fsSL -o bin/aihubshell https://api.aihub.or.kr/api/aihubshell.do && chmod +x bin/aihubshell"
        )
    return p


def _api_key() -> str:
    key = os.environ.get("AIHUB_API_KEY") or get_auto_settings().aihub_api_key
    if not key:
        raise RuntimeError(
            ".env 에 AIHUB_API_KEY 필요. https://aihub.or.kr 마이페이지에서 발급 후 등록."
        )
    return key


def _raw_dir(datasetkey: int) -> Path:
    base = get_settings().ingest_raw_dir / "auto" / "aihub" / str(datasetkey)
    base.mkdir(parents=True, exist_ok=True)
    return base


def list_dataset(datasetkey: int) -> str:
    """데이터셋의 파일 목록 (filekey 포함) 조회. 다운로드 승인 안 돼도 가능."""
    cmd = [str(_aihubshell_path()), "-mode", "l",
           "-datasetkey", str(datasetkey),
           "-aihubapikey", _api_key()]
    log.info("[aihub] list dataset=%s", datasetkey)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log.warning("[aihub] list failed: %s", r.stderr[:500])
    return r.stdout


def download(datasetkey: int, filekeys: list[int] | None = None,
             *, dry_run: bool = False) -> dict:
    """데이터셋(또는 일부 파일) 다운로드. 다운로드 승인 완료된 데이터셋만 가능.

    aihubshell 가 자동으로 분할 파일 (.part0/.part1073741824 …) 병합 + 압축 해제.
    출력 위치는 CWD 라 본 함수는 datasetkey 별 raw_dir 로 cd 한 뒤 호출.
    """
    out_dir = _raw_dir(datasetkey)
    ckpt = CheckpointStore(_SOURCE)
    key = f"{datasetkey}|" + (",".join(str(f) for f in (filekeys or [])) or "ALL")

    if ckpt.is_done(key):
        log.info("[aihub] skip %s (checkpoint done)", key)
        return {"skipped": True, "key": key}

    cmd = [str(_aihubshell_path()), "-mode", "d",
           "-datasetkey", str(datasetkey),
           "-aihubapikey", _api_key()]
    if filekeys:
        cmd += ["-filekey", ",".join(str(f) for f in filekeys)]

    if dry_run:
        log.info("[aihub] DRY-RUN cmd=%s out_dir=%s", " ".join(cmd[:6] + ["-aihubapikey", "***"]), out_dir)
        return {"dry_run": True, "cmd": cmd, "out_dir": str(out_dir)}

    log.info("[aihub] downloading dataset=%s filekeys=%s → %s",
             datasetkey, filekeys or "ALL", out_dir)
    r = subprocess.run(cmd, cwd=out_dir, timeout=24 * 3600)
    if r.returncode != 0:
        log.error("[aihub] download failed rc=%s", r.returncode)
        ckpt.mark_failed(key, f"rc={r.returncode}")
        return {"failed": True, "rc": r.returncode}

    ckpt.mark_done(key, {"datasetkey": datasetkey,
                          "filekeys": filekeys,
                          "out_dir": str(out_dir)})
    return {"datasetkey": datasetkey, "filekeys": filekeys, "out_dir": str(out_dir)}


def main() -> None:
    ap = argparse.ArgumentParser(prog="autograph.ingestion.aihub")
    ap.add_argument("--dataset", type=int, required=True,
                    help="AI Hub dataSetSn (예: 578, 71347)")
    ap.add_argument("--filekeys", help="콤마 구분 filekey (e.g. 57231,57232). 미지정 시 전체 또는 --labels-only")
    ap.add_argument("--labels-only", action="store_true",
                    help="라벨링 데이터 (TL*/VL*) 만 다운 — 화이트리스트 사용 (원본 수백 GB skip)")
    ap.add_argument("--list", action="store_true", help="파일 목록만 조회")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.list:
        print(list_dataset(args.dataset))
        return

    filekeys: list[int] | None = None
    if args.filekeys:
        filekeys = [int(x) for x in args.filekeys.split(",") if x.strip()]
    elif args.labels_only:
        filekeys = _LABEL_FILEKEYS.get(args.dataset)
        if not filekeys:
            ap.error(f"--labels-only 매핑 미정의: dataset={args.dataset}. "
                     f"_LABEL_FILEKEYS 에 추가하거나 --filekeys 직접 지정.")

    result = download(args.dataset, filekeys, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
