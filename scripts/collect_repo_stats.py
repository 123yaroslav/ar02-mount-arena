"""Собирает распределения деревьев файлов с открытых репозиториев GitHub.

Берет по несколько репозиториев на каждый язык, читает дерево через git trees API
(пути и размеры файлов без клонирования) и сохраняет сводную статистику, из которой
генератор строит синтетические деревья.

uv run python scripts/collect_repo_stats.py --per-language 8 --out case/dataset/stats/repo_stats.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

LANGUAGES = ["Python", "TypeScript", "JavaScript", "Go", "Rust", "Java", "C++", "Ruby"]
QUANTILES = 200
MAX_FILES = 20000


def gh(path: str) -> dict:
    out = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def pick_repos(language: str, n: int, rng: random.Random) -> list[dict]:
    query = f"language:{language}+stars:50..5000+pushed:>2026-01-01+size:<300000+fork:false"
    found = gh(f"search/repositories?q={query}&sort=updated&per_page=50")["items"]
    rng.shuffle(found)
    return found[:n]


def quantiles(values: list[float], k: int = QUANTILES) -> list[float]:
    values = sorted(values)
    if not values:
        return []
    return [values[min(len(values) - 1, int(i * len(values) / k))] for i in range(k)] + [values[-1]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-language", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20261106)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--raw", type=Path, default=None, help="куда сохранить сырые деревья (не коммитится)")
    args = parser.parse_args()
    rng = random.Random(args.seed)

    repos = []
    raw = []
    for language in LANGUAGES:
        for repo in pick_repos(language, args.per_language * 2, rng):
            if sum(1 for r in repos if r["language"] == language) >= args.per_language:
                break
            name = repo["full_name"]
            try:
                tree = gh(f"repos/{name}/git/trees/{repo['default_branch']}?recursive=1")
            except subprocess.CalledProcessError:
                continue
            if tree.get("truncated"):
                continue
            blobs = [t for t in tree["tree"] if t["type"] == "blob"]
            if not 10 <= len(blobs) <= MAX_FILES:
                continue
            files = [(t["path"], int(t.get("size", 0))) for t in blobs]
            dirs = {str(PurePosixPath(p).parent) for p, _ in files}
            repos.append({"name": name, "language": language, "files": files, "dirs": sorted(dirs)})
            raw.append({"name": name, "language": language, "files": files})
            print(f"{language:10s} {name:50s} files={len(files)}", file=sys.stderr)

    depth_counts: Counter[int] = Counter()
    ext_counts: Counter[str] = Counter()
    sizes_text: list[float] = []
    sizes_binary: list[float] = []
    files_per_dir: list[float] = []
    subdirs_per_dir: list[float] = []
    file_counts = []
    largest = []
    text_ext = set(json.loads(Path(__file__).with_name("text_extensions.json").read_text()))
    for repo in repos:
        file_counts.append(len(repo["files"]))
        per_dir: Counter[str] = Counter()
        children: dict[str, set[str]] = {}
        for path, size in repo["files"]:
            p = PurePosixPath(path)
            depth_counts[len(p.parts) - 1] += 1
            ext = p.suffix.lower() or p.name.lower()
            ext_counts[ext] += 1
            (sizes_text if ext in text_ext else sizes_binary).append(max(size, 0))
            per_dir[str(p.parent)] += 1
            parts = p.parts[:-1]
            for i in range(len(parts)):
                parent = "/".join(parts[:i]) or "."
                children.setdefault(parent, set()).add("/".join(parts[: i + 1]))
        all_dirs = set(per_dir) | set(children) | {"."}
        files_per_dir.extend(per_dir.get(d, 0) for d in all_dirs)
        subdirs_per_dir.extend(len(children.get(d, ())) for d in all_dirs)
        largest.append(max(size for _, size in repo["files"]))

    total = sum(depth_counts.values())
    stats = {
        "source": "GitHub git trees API, default branch, без .git",
        "seed": args.seed,
        "repos": [{"name": r["name"], "language": r["language"], "files": len(r["files"])} for r in repos],
        "file_count_per_repo": sorted(file_counts),
        "depth_probability": {str(d): c / total for d, c in sorted(depth_counts.items())},
        "files_per_dir_quantiles": quantiles(files_per_dir),
        "subdirs_per_dir_quantiles": quantiles(subdirs_per_dir),
        "text_fraction": len(sizes_text) / max(1, len(sizes_text) + len(sizes_binary)),
        "size_text_quantiles": quantiles(sizes_text),
        "size_binary_quantiles": quantiles(sizes_binary),
        "largest_file_per_repo": sorted(largest),
        "top_extensions": [[e, c / total] for e, c in ext_counts.most_common(40)],
        "summary": {
            "repos": len(repos),
            "files_total": total,
            "median_files_per_repo": sorted(file_counts)[len(file_counts) // 2],
            "median_text_size": quantiles(sizes_text)[QUANTILES // 2],
            "median_depth": sorted(depth_counts.elements())[total // 2],
            "log10_mean_size": sum(math.log10(s + 1) for s in sizes_text + sizes_binary) / total,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(stats, ensure_ascii=False, indent=1))
    if args.raw:
        args.raw.parent.mkdir(parents=True, exist_ok=True)
        with args.raw.open("w") as f:
            for r in raw:
                f.write(json.dumps(r) + "\n")
    print(json.dumps(stats["summary"], indent=1), file=sys.stderr)


if __name__ == "__main__":
    main()
