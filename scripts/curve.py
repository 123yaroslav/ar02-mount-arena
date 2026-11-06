"""Кривая эволюции и AUC по журналу кампании, по правилам common/search-history.md арены.

Журнал это JSONL, одна запись на предложенное решение.
{"order": 1, "id": "r1-c1", "parents": ["baseline"], "proposed_at": "2026-11-06T00:43",
 "finished_at": "2026-11-06T01:09", "public_v1": 23.2, "valid_v1": true, ...}

Результат кандидата ставится в точку X, равную числу уже предложенных решений в момент
завершения его проверки (proposal_count_at_finish). Поздний результат не улучшает кривую
задним числом. Значение держится до следующего события (left_step). От нуля до первого
события действует baseline. AUC это площадь под ступенчатой кривой на горизонте, равном
лимиту решений.

uv run python scripts/curve.py results/validation-opus/journal.jsonl --limit 4 --key public_v1 --valid-key valid_v1 --out results/figures/curve_validation_v1.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

INK, MUTED, GRID, SURFACE, SERIES = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb", "#2a78d6"


def at_finish(entries: list[dict]) -> list[tuple[int, dict]]:
    """Пары (число предложений к моменту завершения проверки, запись) в порядке завершения."""
    done = sorted(entries, key=lambda e: (e["finished_at"], e["order"]))
    return [(sum(1 for o in entries if o["proposed_at"] <= e["finished_at"]), e) for e in done]


def curve(entries: list[dict], baseline: float, limit: int, key: str, valid_key: str) -> tuple[list[float], float]:
    """Лучшее известное качество в точках 0..limit и AUC по левым ступеням."""
    events = at_finish(entries)
    best = baseline
    values = [baseline]
    for x in range(1, limit + 1):
        for ex, e in events:
            if ex == x and e.get(valid_key) and e.get(key) is not None:
                best = max(best, float(e[key]))
        values.append(best)
    auc = sum(values[:-1])  # значение в точке x действует на отрезке от x до x + 1
    return values, auc


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("journal", type=Path)
    p.add_argument("--limit", type=int, required=True)
    p.add_argument("--baseline", type=float, default=1.0)
    p.add_argument("--key", required=True, help="поле журнала с качеством, например public_v1")
    p.add_argument("--valid-key", required=True, help="поле журнала с допуском, например valid_v1")
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    entries = [json.loads(line) for line in a.journal.read_text().splitlines() if line.strip()]
    values, auc = curve(entries, a.baseline, a.limit, a.key, a.valid_key)
    points = [{"id": e["id"], "proposal_count_at_finish": x} for x, e in at_finish(entries)]
    print(json.dumps({"key": a.key, "points": points, "curve": values, "auc": auc, "mean_quality": auc / a.limit, "final_quality": values[-1]}))
    if a.out:
        xs = list(range(0, a.limit + 1))
        ys = values
        fig, ax = plt.subplots(figsize=(6.5, 3.6), dpi=160)
        fig.patch.set_facecolor(SURFACE)
        ax.set_facecolor(SURFACE)
        ax.step(xs + [a.limit + 0.0001], ys + [ys[-1]], where="post", color=SERIES, linewidth=2)
        for x, e in at_finish(entries):
            y = float(e[a.key]) if e.get(a.valid_key) and e.get(a.key) is not None else 0.0
            ax.plot([x], [y], marker="o", markersize=7, color=SERIES if e.get(a.valid_key) else MUTED, markeredgecolor=SURFACE, markeredgewidth=2)
            ax.annotate(e["id"], (x, y), textcoords="offset points", xytext=(6, -12), fontsize=7, color=MUTED)
        ax.set_xlabel("число предложенных решений к концу проверки", color=MUTED, fontsize=8)
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
