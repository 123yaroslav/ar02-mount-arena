"""Опыт ADR-0009. Прогон кандидата на public в копии симулятора с калибровкой CPU.

Копия репозитория с примененным calibration.patch, запуск из ее корня.
python results/checks/cpu_normalization/run.py <кандидат> <параллельность> <выход.jsonl>
"""

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))


def run(args):
    from mountsim.runner import run_instance

    spec, cand = args
    r = run_instance(spec, cand)
    return {k: r.get(k) for k in ["ok", "fs_wait", "cpu_client", "cpu_server", "cal_client", "cal_server", "total"]}


def main():
    cand, jobs, out = str(Path(sys.argv[1]).resolve()), int(sys.argv[2]), sys.argv[3]
    rows = [json.loads(line) for line in open(Path.home() / ".ar02/data/public.jsonl")]
    with ProcessPoolExecutor(jobs) as pool:
        res = list(pool.map(run, [(json.loads(r["input"]), cand) for r in rows]))
    with open(out, "w") as f:
        for r, x in zip(rows, res):
            f.write(json.dumps({"id": r["id"], "baseline_total": json.loads(r["reference"])["baseline_total"], **x}) + "\n")


if __name__ == "__main__":
    main()
