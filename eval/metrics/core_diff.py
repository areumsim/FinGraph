"""PRD §10.12 — AutoNexusGraph 코어 코드 변경 < 5% 자동 측정.

기준점 (baseline):
    AutoGraph 패키지 (src/autograph) 가 처음 등장한 commit 의 직전 commit.
    그 시점에 코어 패키지는 ``src/fingraph`` 였고 이후 ``src/autonexusgraph`` 로
    리네임됨. 본 메트릭은 두 경로의 rename 을 감지해 누적 LOC 변경을 측정한다.

측정:
    baseline_loc     = baseline 시점의 코어 패키지 .py 총 LOC
    current_loc      = HEAD 시점의 코어 패키지 .py 총 LOC
    changed_loc      = git diff -M (added + deleted)
    change_ratio     = changed_loc / baseline_loc
    target_met       = change_ratio < 0.05

CLI:
    python -m eval.metrics.core_diff
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any


log = logging.getLogger(__name__)


TARGET_RATIO = 0.05   # PRD §10.12 — 코어 변경 < 5%.

# 본 repo 의 코어 패키지 경로 — rename 전/후 모두 추적.
_CORE_PATHS = ("src/fingraph", "src/autonexusgraph")
_AUTOGRAPH_PATH = "src/autograph"


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()


def _baseline_commit() -> str | None:
    """AutoGraph 가 처음 추가된 commit 의 직전 (= 부모) commit."""
    try:
        first_autograph = _run([
            "git", "log", "--reverse", "--diff-filter=A",
            "--format=%H", "--", _AUTOGRAPH_PATH,
        ]).split("\n")[0].strip()
        if not first_autograph:
            return None
        return _run(["git", "rev-parse", f"{first_autograph}^"])
    except subprocess.CalledProcessError:
        return None


def _loc_at(commit: str, path: str) -> int:
    """commit 시점에 path 하위 .py 파일 총 LOC."""
    try:
        files = _run(["git", "ls-tree", "-r", "--name-only", commit, path])
    except subprocess.CalledProcessError:
        return 0
    total = 0
    for f in files.split("\n"):
        if not f.endswith(".py"):
            continue
        try:
            content = _run(["git", "show", f"{commit}:{f}"])
            total += content.count("\n") + (0 if content.endswith("\n") else 1)
        except subprocess.CalledProcessError:
            continue
    return total


def _diff_loc(baseline: str, head: str, paths: tuple[str, ...]) -> tuple[int, int]:
    """rename 감지 git diff --numstat → (added, deleted) 합."""
    try:
        out = _run(["git", "diff", "--numstat", "-M", "-B", baseline, head, "--", *paths])
    except subprocess.CalledProcessError:
        return 0, 0
    added = deleted = 0
    for line in out.split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        a, d = parts[0], parts[1]
        if a.isdigit():
            added += int(a)
        if d.isdigit():
            deleted += int(d)
    return added, deleted


def collect_core_diff() -> dict[str, Any]:
    """PRD §10.12 측정.

    Returns:
        {
          "baseline_commit": "abc123",
          "baseline_loc":   12025,
          "current_loc":    12383,
          "added":          448,
          "deleted":        90,
          "changed_loc":    538,
          "change_ratio":   0.0447,
          "target_ratio":   0.05,
          "target_met":     True,
          "available":      True,
        }
    """
    out: dict[str, Any] = {
        "available":       False,
        "baseline_commit": None,
        "baseline_loc":    0,
        "current_loc":     0,
        "added":           0,
        "deleted":         0,
        "changed_loc":     0,
        "change_ratio":    0.0,
        "target_ratio":    TARGET_RATIO,
        "target_met":      False,
    }
    base = _baseline_commit()
    if not base:
        log.warning("[core_diff] baseline commit 미발견 (AutoGraph 도입 commit 없음)")
        return out

    baseline_loc = 0
    for p in _CORE_PATHS:
        baseline_loc += _loc_at(base, p)

    current_loc = 0
    for p in _CORE_PATHS:
        try:
            files = _run(["git", "ls-tree", "-r", "--name-only", "HEAD", p])
        except subprocess.CalledProcessError:
            continue
        for f in files.split("\n"):
            if not f.endswith(".py"):
                continue
            try:
                content = _run(["git", "show", f"HEAD:{f}"])
                current_loc += content.count("\n") + (0 if content.endswith("\n") else 1)
            except subprocess.CalledProcessError:
                continue

    added, deleted = _diff_loc(base, "HEAD", _CORE_PATHS)
    changed = added + deleted
    ratio = (changed / baseline_loc) if baseline_loc else 0.0

    out.update({
        "available":       True,
        "baseline_commit": base,
        "baseline_loc":    baseline_loc,
        "current_loc":     current_loc,
        "added":           added,
        "deleted":         deleted,
        "changed_loc":     changed,
        "change_ratio":    round(ratio, 4),
        "target_met":      ratio < TARGET_RATIO,
    })
    return out


def format_summary_md(d: dict[str, Any]) -> str:
    lines = ["## 코어 코드 변경량 (PRD §10.12)"]
    if not d.get("available"):
        lines.append("- (git 미가용 또는 baseline 미발견)")
        return "\n".join(lines)

    mark = "✅" if d["target_met"] else "❌"
    lines.append(
        f"- baseline: `{d['baseline_commit'][:10]}` (AutoGraph 도입 직전)"
    )
    lines.append(
        f"- baseline LOC: **{d['baseline_loc']:,}** | HEAD LOC: **{d['current_loc']:,}**"
    )
    lines.append(
        f"- 누적 변경: **+{d['added']:,} / -{d['deleted']:,} = {d['changed_loc']:,} LOC**"
    )
    lines.append(
        f"- 변경 비율: **{d['change_ratio'] * 100:.2f}%** "
        f"(목표 < {d['target_ratio'] * 100:.0f}%) {mark}"
    )
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level="INFO")
    print(format_summary_md(collect_core_diff()))


if __name__ == "__main__":
    main()


__all__ = [
    "TARGET_RATIO", "collect_core_diff", "format_summary_md",
]
