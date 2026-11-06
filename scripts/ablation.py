"""Абляция симулятора v2. Какие изменения v2 убирают выигрыш кандидата.

Гоняет baseline и кандидата на подмножестве деревьев train при четырех настройках.
Джиттер сети и сдвиг часов сторон включаются и выключаются, генератор текста остается v2.
Печатает ускорение кандидата на валидных экземплярах и число нарушений.

uv run python scripts/ablation.py results/validation-opus/candidates/r1-c1 --trees t00,t03
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASELINE = ROOT / "case" / "ways" / "baseline"


def init(jitter: bool, clocks: bool) -> None:
    from mountsim import netmodel, runner

    if not jitter:
        netmodel.JITTER = 0.0
    if not clocks:
        orig = runner.Side.__init__

        def same_clock(self, ctx, side, cand, config, tree, offset):
            orig(self, ctx, side, cand, config, tree, 0.0)

        runner.Side.__init__ = same_clock


def run(args: tuple[dict, str]) -> tuple[bool, float | None, str]:
    from mountsim.runner import run_instance

    spec, cand = args
    r = run_instance(spec, cand)
    return r["ok"], r.get("total"), (r["violations"] or [""])[0][:120]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("candidate", type=Path)
    p.add_argument("--trees", default="t00,t03")
    p.add_argument("--jobs", type=int, default=8)
    a = p.parse_args()
    trees = a.trees.split(",")
    rows = [json.loads(line) for line in (ROOT / "case" / "dataset" / "train.jsonl").read_text().splitlines()]
    rows = [r for r in rows if r["id"].split("-")[1] in trees]
    specs = [json.loads(r["input"]) for r in rows]
    cand = str(a.candidate.resolve())
    for jitter, clocks in [(True, True), (False, True), (True, False), (False, False)]:
        with ProcessPoolExecutor(a.jobs, initializer=init, initargs=(jitter, clocks)) as pool:
            base = list(pool.map(run, [(s, str(BASELINE)) for s in specs]))
            got = list(pool.map(run, [(s, cand) for s in specs]))
        logs = [math.log2(b[1] / c[1]) for b, c in zip(base, got) if b[0] and c[0]]
        invalid = [rows[i]["id"] for i, c in enumerate(got) if not c[0]]
        speedup = round(2 ** (sum(logs) / len(logs)), 3) if logs else None
        print(json.dumps({"candidate": a.candidate.name, "trees": trees, "jitter": jitter, "clock_offsets": clocks,
                          "examples": len(rows), "invalid": len(invalid), "speedup_on_valid": speedup}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
