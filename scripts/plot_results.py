"""График ускорения по сценариям для нескольких кандидатов.

uv run python scripts/plot_results.py --out results/figures/speedup_by_kind.png \
    "классический=results/classic/train.jsonl" "r1-c2=results/validation-opus/r1-c2/train.jsonl"
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]  # категориальные слоты 1-5
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
KINDS = ["walk", "grep", "small_reads", "big_file", "writes", "session"]
KIND_RU = {"walk": "обход дерева", "grep": "grep", "small_reads": "группы файлов", "big_file": "большой файл", "writes": "записи", "session": "сессия агента"}


def by_kind(path: Path) -> dict[str, float]:
    groups = defaultdict(list)
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if r["statistics"]["valid"]:
            groups[r["statistics"]["kind"]].append(r["score"])
    return {k: 2 ** (sum(v) / len(v)) for k, v in groups.items()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("series", nargs="+", help="имя=путь к results/*.jsonl")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    data = [(s.split("=", 1)[0], by_kind(Path(s.split("=", 1)[1]))) for s in a.series]
    n = len(data)
    fig, ax = plt.subplots(figsize=(8.5, 4.8), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    height = 0.8 / n
    for i, (name, vals) in enumerate(data):
        ys = [k_i + (i - (n - 1) / 2) * height for k_i in range(len(KINDS))]
        xs = [math.log2(vals.get(k, 1.0)) for k in KINDS]
        ax.barh(ys, xs, height=height * 0.85, color=SERIES[i], label=name, edgecolor=SURFACE, linewidth=1)
        for y, x, k in zip(ys, xs, KINDS):
            ax.text(x + 0.04, y, f"{vals.get(k, 1.0):.2f}".replace(".", ","), va="center", fontsize=7, color=MUTED)
    ax.axvline(0, color=INK, linewidth=0.8)
    ax.set_yticks(range(len(KINDS)), [KIND_RU[k] for k in KINDS], color=INK, fontsize=9)
    ax.invert_yaxis()
    top = max(math.log2(v) for _, vals in data for v in vals.values())
    ticks = list(range(0, int(math.ceil(top)) + 1))
    ax.set_xticks(ticks, [f"x{2 ** t}" for t in ticks], color=MUTED, fontsize=8)
    ax.set_xlim(-0.1, max(ticks) + 0.4)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ["top", "right", "left"]:
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    ax.set_xlabel("ускорение относительно baseline, train (логарифмическая шкала)", color=MUTED, fontsize=8)
    ax.legend(frameon=False, fontsize=8, loc="lower right", labelcolor=INK)
    fig.tight_layout(pad=1.2)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, facecolor=SURFACE)
    print(a.out)


if __name__ == "__main__":
    main()
