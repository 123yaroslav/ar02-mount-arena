"""Кривая эволюции и AUC по журналу кампании, по правилам common/search-history.md арены.

Журнал это JSONL, одна запись на предложенное решение, в порядке предложения.
{"order": 1, "id": "r1-c1", "parents": ["baseline"], "researcher": "r1", "hypothesis": "...",
 "change": "...", "train": 1.9, "public": 1.8, "valid": true}

Кривая по оси X это номер предложения, по оси Y лучший public на этот момент.
Значение держится до следующего события. От нуля до первого события действует baseline.
AUC это площадь под ступенчатой кривой на горизонте, равном лимиту решений.

uv run python scripts/curve.py results/validation-opus/journal.jsonl --limit 4 --out results/figures/curve_validation.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

INK, MUTED, GRID, SURFACE, SERIES = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb", "#2a78d6"


def curve(entries: list[dict], baseline: float, limit: int, key: str, valid_key: str = "valid") -> tuple[list[float], float]:
    best = baseline
    values = []
    for i in range(1, limit + 1):
        for e in entries:
            if e["order"] == i and e.get(valid_key) and e.get(key) is not None:
                best = max(best, float(e[key]))
        values.append(best)
    auc = baseline + sum(values[:-1])  # шаг от 0 до 1 держит baseline, дальше значения после событий
    return values, auc


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("journal", type=Path)
    p.add_argument("--limit", type=int, required=True)
    p.add_argument("--baseline", type=float, default=1.0)
    p.add_argument("--key", default="public")
    p.add_argument("--valid-key", default="valid")
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    entries = [json.loads(line) for line in a.journal.read_text().splitlines() if line.strip()]
    values, auc = curve(entries, a.baseline, a.limit, a.key, a.valid_key)
    print(json.dumps({"key": a.key, "curve": values, "auc": auc, "mean_quality": auc / a.limit, "final_quality": values[-1]}))
    if a.out:
        xs = [0] + list(range(1, a.limit + 1))
        ys = [a.baseline] + values
        fig, ax = plt.subplots(figsize=(6.5, 3.6), dpi=160)
        fig.patch.set_facecolor(SURFACE)
        ax.set_facecolor(SURFACE)
        ax.step(xs + [a.limit + 0.0001], ys + [ys[-1]], where="post", color=SERIES, linewidth=2)
        for e in entries:
            y = float(e[a.key]) if e.get(a.valid_key) and e.get(a.key) is not None else 0.0
            ax.plot([e["order"]], [y], marker="o", markersize=7, color=SERIES if e.get(a.valid_key) else MUTED, markeredgecolor=SURFACE, markeredgewidth=2)
            ax.annotate(e["id"], (e["order"], y), textcoords="offset points", xytext=(6, -12), fontsize=7, color=MUTED)
        ax.set_xlabel("номер предложенного решения", color=MUTED, fontsize=8)
        ax.set_ylabel(f"лучший {a.key}, ускорение", color=MUTED, fontsize=8)
        ax.set_xticks(range(0, a.limit + 1))
        ax.grid(color=GRID, linewidth=0.6)
        for side in ["top", "right"]:
            ax.spines[side].set_visible(False)
        for side in ["left", "bottom"]:
            ax.spines[side].set_color(GRID)
        ax.tick_params(colors=MUTED, labelsize=8, length=0)
        ax.set_ylim(bottom=0)
        fig.tight_layout(pad=1.2)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(a.out, facecolor=SURFACE)


if __name__ == "__main__":
    main()
