"""Проверочная кампания: public на симуляторе v1 и на v2 для каждого кандидата.

uv run python scripts/plot_validation.py results/validation-opus/journal.jsonl --out results/figures/validation_v1_v2.png
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

V1, V2 = "#2a78d6", "#eb6834"
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("journal", type=Path)
    p.add_argument("--classic", type=float, default=1.747, help="классический протокол на public v2")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    rows = [json.loads(line) for line in a.journal.read_text().splitlines() if line.strip()]
    fig, ax = plt.subplots(figsize=(8.5, 4.4), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    h = 0.36
    for i, r in enumerate(rows):
        v1 = math.log2(r["public_v1"])
        ax.barh(i - h / 2, v1, height=h * 0.9, color=V1, label="симулятор v1" if i == 0 else None)
        ax.text(v1 + 0.06, i - h / 2, f"{r['public_v1']:.2f}".replace(".", ","), va="center", fontsize=7, color=MUTED)
        if r["valid_v2"]:
            v2 = math.log2(r["public_v2"])
            ax.barh(i + h / 2, v2, height=h * 0.9, color=V2, label="симулятор v2" if i == 0 else None)
            ax.text(v2 + 0.06, i + h / 2, f"{r['public_v2']:.2f}".replace(".", ","), va="center", fontsize=7, color=MUTED)
        else:
            ax.text(math.log2(a.classic) + 0.12, i + h / 2, f"на v2 не допущен, {r['v2_reason']}", va="center", fontsize=7, color=INK)
    ax.axvline(math.log2(a.classic), color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
    ax.text(math.log2(a.classic) + 0.05, -0.62, "классический протокол на v2, 1,75", fontsize=7, color=MUTED)
    ax.axvline(0, color=INK, linewidth=0.8)
    ax.set_yticks(range(len(rows)), [f"{r['order']}. {r['id']}" for r in rows], fontsize=9, color=INK)
    ticks = [0, 1, 2, 3, 4, 5]
    ax.set_xticks(ticks, [f"x{2 ** t}" for t in ticks], fontsize=8, color=MUTED)
    ax.set_xlim(-0.1, 5.6)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ["top", "right", "left"]:
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    ax.set_xlabel("столбцы это ускорение относительно baseline на public при симуляторе v1 (логарифмическая шкала)", fontsize=8, color=MUTED)
    if any(r["valid_v2"] for r in rows):
        ax.legend(frameon=False, fontsize=8, loc="lower right")
    ax.set_ylim(len(rows) - 0.4, -0.8)
    fig.tight_layout(pad=1.2)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, facecolor=SURFACE)


if __name__ == "__main__":
    main()
