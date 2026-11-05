"""Генератор экземпляров AR-02. Один код и одни параметры для всех split, меняется только сид.

Экземпляр это дерево x сценарий x профиль сети. Семейство это дерево: все экземпляры
одного дерева попадают в один split. Сид train открыт. Сиды public и private знает
только оператор, иначе исследователь мог бы восстановить скрытые деревья.

uv run python case/dataset/generate.py --split train --seed 1 --trees 8 --out case/dataset/train.jsonl
uv run python case/dataset/generate.py --split public --seed-file ~/.ar02/seeds.json --trees 4 --out <вне репозитория>

Поле reference заполняется прогоном baseline на том же экземпляре (флаг --reference).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mountsim.runner import run_instance  # noqa: E402
from mountsim.scenario import KINDS, READ_ONLY_KINDS  # noqa: E402

RTT_MS = [1, 20, 80, 200]
LOSS = [0.0, 0.01]
BANDWIDTH_MBIT = 50
STALENESS = 1.0
BASELINE = ROOT / "case" / "ways" / "baseline"
FORMAT = "ar02-v2"


def make_examples(split: str, seed: int, trees: int) -> list[dict]:
    rng = random.Random(f"{split}:{seed}")
    out = []
    for t in range(trees):
        tree_seed = rng.randrange(1 << 30)
        for kind in KINDS:
            scenario_seed = rng.randrange(1 << 30)
            for rtt in RTT_MS:
                for loss in LOSS:
                    spec = {
                        "format": FORMAT,
                        "tree": {"seed": tree_seed, "content_seed": rng.randrange(1 << 30)},
                        "scenario": {"kind": kind, "seed": scenario_seed},
                        "profile": {"rtt_ms": rtt, "loss": loss, "bandwidth_mbit": BANDWIDTH_MBIT},
                        "mode": "ro" if kind in READ_ONLY_KINDS else "rw",
                        "staleness_seconds": STALENESS,
                    }
                    ex_id = f"{split}-t{t:02d}-{kind}-rtt{rtt}-loss{int(loss * 100)}"
                    out.append({"id": ex_id, "input": json.dumps(spec, sort_keys=True), "family": f"{split}-t{t:02d}"})
    return out


def reference(example: dict) -> dict:
    report = run_instance(json.loads(example["input"]), BASELINE)
    if not report["ok"]:
        raise RuntimeError(f"baseline нарушил гейт на {example['id']}: {report['violations']}")
    ref = {"baseline_total": report["total"], "ops": report["ops"], "final_digest": report["final_digest"], "format": FORMAT}
    return {**example, "reference": json.dumps(ref, sort_keys=True)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--split", choices=["train", "public", "private"], required=True)
    p.add_argument("--seed", type=int)
    p.add_argument("--seed-file", type=Path, help="JSON {split: seed}, хранится у оператора")
    p.add_argument("--trees", type=int, required=True)
    p.add_argument("--reference", action="store_true", help="прогнать baseline и записать reference")
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    if a.seed is None:
        if a.seed_file is None:
            p.error("нужен --seed или --seed-file")
        a.seed = int(json.loads(a.seed_file.expanduser().read_text())[a.split])
    if a.split != "train" and a.seed_file is None:
        print("предупреждение: сид public/private передан в командной строке", file=sys.stderr)
    examples = make_examples(a.split, a.seed, a.trees)
    if a.reference:
        with ProcessPoolExecutor(a.jobs) as pool:
            examples = list(pool.map(reference, examples, chunksize=4))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w") as f:
        for ex in examples:
            row = {"id": ex["id"], "input": ex["input"]}
            if "reference" in ex:
                row["reference"] = ex["reference"]
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    seed_hash = hashlib.sha256(f"{a.split}:{a.seed}".encode()).hexdigest()[:16]
    print(json.dumps({"split": a.split, "examples": len(examples), "trees": a.trees, "seed_sha256_16": seed_hash}), file=sys.stderr)


if __name__ == "__main__":
    main()
