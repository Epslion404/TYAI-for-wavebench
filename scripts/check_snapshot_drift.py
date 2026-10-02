#!/usr/bin/env python3
"""Check whether the packaged snapshot under 比赛区/ still matches the root tree.

Read-only: it compares blob hashes in a git ref and never writes anything.

The package carries a frozen copy of the Core tree (`比赛区/天翼AI电子科研Skill/wavebench/`),
so it drifts the moment the root moves. This script makes that gap visible instead of
relying on someone remembering; 手工同步 by whole-directory overwrite remains the rule.

Exit codes: 0 = in sync (or no snapshot in the ref), 1 = drift found.
"""

from __future__ import annotations

import argparse
import subprocess

SNAPSHOT_PREFIX = "比赛区/天翼AI电子科研Skill/wavebench/"
# 这些根目录条目按设计不进包内快照：包只装 Core 快照，不装比赛区自身、仓库外试验脚本与 contributor 说明
ROOT_ONLY_BY_DESIGN = ("比赛区/", "tool-of-rei/", "测试区/", "AGENTS.md")


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def tree_of(ref: str) -> dict[str, str]:
    tree: dict[str, str] = {}
    for line in git("ls-tree", "-r", ref).splitlines():
        if not line:
            continue
        meta, path = line.split("\t", 1)
        tree[path] = meta.split()[2]
    return tree


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Git ref to inspect (default: HEAD)")
    parser.add_argument("--quiet", action="store_true", help="Only print the verdict")
    args = parser.parse_args()

    tree = tree_of(args.ref)
    snapshot = {
        path[len(SNAPSHOT_PREFIX):]: sha
        for path, sha in tree.items()
        if path.startswith(SNAPSHOT_PREFIX)
    }
    if not snapshot:
        print(f"no packaged snapshot in {args.ref} (比赛区/ lives on competition only)")
        return 0
    root = {
        path: sha
        for path, sha in tree.items()
        if not path.startswith(SNAPSHOT_PREFIX) and not path.startswith(ROOT_ONLY_BY_DESIGN)
    }

    missing = sorted(set(root) - set(snapshot))  # 根有快照无：漏同步
    stale = sorted(path for path in set(root) & set(snapshot) if root[path] != snapshot[path])
    extra = sorted(set(snapshot) - set(root))  # 快照有根无：冗余或重命名

    if not args.quiet:
        print(f"snapshot: {SNAPSHOT_PREFIX}")
        print(f"ref: {args.ref}  |  root {len(root)} files  |  snapshot {len(snapshot)} files")
        for label, paths in (
            ("漏同步（根有快照无）", missing),
            ("内容落后", stale),
            ("快照多余（根没有）", extra),
        ):
            if not paths:
                continue
            print(f"\n{label}: {len(paths)}")
            for path in paths[:40]:
                print(f"  {path}")
            if len(paths) > 40:
                print(f"  ... 另有 {len(paths) - 40} 个")

    drift = len(missing) + len(stale)
    if drift:
        print(f"\nDRIFT: {len(missing)} 个漏同步 + {len(stale)} 个内容落后；整目录覆盖后重跑包内测试")
        return 1
    print("\nIN SYNC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
