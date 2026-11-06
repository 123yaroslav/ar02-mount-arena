"""Сырой и нормированный счет по прогонам run.py. Эталон нормировки это baseline при 4 прогонах.

python results/checks/cpu_normalization/analyze.py <каталог с прогонами>
"""

import json
import math
import statistics as st
import sys
from pathlib import Path

D = Path(sys.argv[1])
RUNS = ["baseline_j4_a", "baseline_j9_a", "baseline_j14_a", "classic_j4_a", "classic_j14_a",
        "r1c2_j4_a", "r1c2_j4_b", "r1c2_j9_a", "r1c2_j9_b", "r1c2_j14_a"]


def load(name):
    p = D / f"{name}.jsonl"
    return {json.loads(line)["id"]: json.loads(line) for line in open(p)} if p.exists() else None


base = load("baseline_j4_a")
REF = st.median([x[k] for x in base.values() for k in ("cal_client", "cal_server")])


def norm(x):
    return x["fs_wait"] + x["cpu_client"] * REF / x["cal_client"] + x["cpu_server"] * REF / x["cal_server"]


for name in RUNS:
    run = load(name)
    if run is None:
        continue
    raw = [math.log2(x["baseline_total"] / x["total"]) for x in run.values() if x["ok"]]
    nrm = [math.log2(norm(base[i]) / norm(x)) for i, x in run.items() if x["ok"]]
    cal = st.median([x["cal_client"] for x in run.values()])
    print(json.dumps({"run": name, "valid": len(raw), "raw": round(2 ** st.mean(raw), 4),
                      "normalized": round(2 ** st.mean(nrm), 4), "median_cal_ms": round(cal * 1000, 1)}))
