"""Сверка сжимаемости текста генератора с настоящим кодом.

Меряет на наборе папок две величины. Сжатие zlib уровня 6 по файлам и сжатие словарем
строк по всему дереву в порядке обхода grep (строка, уже встречавшаяся в потоке, стоит
2 байта). Вторая величина показывает, насколько строки повторяются между файлами.

uv run python scripts/calibrate_text.py <папка с репозиториями> [--generator-seeds 0 1 2]
"""

from __future__ import annotations

import argparse
import json
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEXT_EXT = set(json.loads((ROOT / "scripts" / "text_extensions.json").read_text()))


def measure(files: list[bytes]) -> dict:
    raw = sum(len(f) for f in files)
    zl = sum(len(zlib.compress(f, 6)) for f in files)
    seen: set[bytes] = set()
    cost = 0
    for f in files:
        for line in f.split(b"\n"):
            if line in seen and len(line) > 2:
                cost += 2
            else:
                seen.add(line)
                cost += len(line) + 1
    return {"files": len(files), "mb": round(raw / 1e6, 2), "zlib6": round(raw / max(1, zl), 2), "line_dict": round(raw / max(1, cost), 2)}


def real_tree(path: Path) -> list[bytes]:
    out = []
    for p in sorted(path.rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        ext = p.suffix.lower() or p.name.lower()
        if ext in TEXT_EXT and p.stat().st_size <= 1 << 20:
            out.append(p.read_bytes())
    return out


def generated_tree(seed: int) -> list[bytes]:
    from mountsim.tree import build_tree

    tree = build_tree({"seed": seed, "content_seed": 1})
    return [tree.fs.read(p, 0, 1 << 30) for p in sorted(tree.text_files)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", type=Path)
    ap.add_argument("--generator-seeds", type=int, nargs="*", default=[6, 9, 16, 18])
    a = ap.parse_args()
    rows = []
    for d in sorted(p for p in a.repos.iterdir() if p.is_dir()):
        rows.append({"tree": d.name, **measure(real_tree(d))})
    for s in a.generator_seeds:
        rows.append({"tree": f"generator-{s}", **measure(generated_tree(s))})
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
