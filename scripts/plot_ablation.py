"""График абляции. Ускорение r1-c1 на двух деревьях train при разных настройках симулятора.

uv run python scripts/plot_ablation.py results/checks/ablation.jsonl --out results/figures/ablation_r1c1.png
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BAR, ACCENT = "#2a78d6", "#eb6834"
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
LABELS = {
    (True, True): "v2 целиком",
    (False, True): "без джиттера",
    (True, False): "общие часы",
    (False, False): "общие часы, без джиттера",
}


def classic_on(trees: list[str]) -> float:
    rows = [json.loads(line) for line in (ROOT / "results" / "v2" / "classic" / "train.jsonl").read_text().splitlines()]
    vals = [r["score"] for r in rows if r["example_id"].split("-")[1] in trees]
    return 2 ** (sum(vals) / len(vals))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("ablation", type=Path)
    p.add_argument("--candidate", default="r1-c1")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    rows = [json.loads(line) for line in a.ablation.read_text().splitlines() if line.strip()]
    rows = [r for r in rows if r["candidate"] == a.candidate]
    classic = classic_on(rows[0]["trees"])
    fig, ax = plt.subplots(figsize=(8.5, 3.6), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for i, r in enumerate(rows):
        x = math.log2(r["speedup_on_valid"])
        color = ACCENT if not r["clock_offsets"] else BAR
        ax.barh(i, x, height=0.6, color=color, edgecolor=SURFACE, linewidth=2)
        ax.text(x + 0.08, i, f"{r['speedup_on_valid']:.1f}".replace(".", ","), va="center", fontsize=12, color=INK)
    ax.axvline(math.log2(classic), color=MUTED, linewidth=1, linestyle=(0, (4, 3)))
    ax.text(math.log2(classic) + 0.06, len(rows) - 0.45, f"классика на тех же деревьях, {classic:.2f}".replace(".", ","), fontsize=11, color=MUTED)
    ax.set_yticks(range(len(rows)), [LABELS[(r["jitter"], r["clock_offsets"])] for r in rows], fontsize=14, color=INK)
    ax.invert_yaxis()
    top = max(math.log2(r["speedup_on_valid"]) for r in rows)
    ticks = list(range(0, int(math.ceil(top)) + 1))
    ax.set_xticks(ticks, [f"x{2 ** t}" for t in ticks], color=MUTED, fontsize=12)
    ax.set_xlim(0, max(ticks) + 0.5)
    ax.set_ylim(len(rows) - 0.4, -0.75)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ["top", "right", "left"]:
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    n = rows[0]["examples"]
    ax.set_xlabel(f"{a.candidate} на {n} экземплярах train, логарифмическая шкала", color=MUTED, fontsize=12)
    fig.tight_layout(pad=1.2)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out, facecolor=SURFACE)


if __name__ == "__main__":
    main()
