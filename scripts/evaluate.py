"""Оценка кандидата на split тем же путем, что в арене.

Для каждого примера запускается solve.py кандидата (stdin JSON, stdout {"answer"}),
затем score.py, в конце aggregate.py. Печатает итоговый score и разбивку по сценариям
и профилям. Сырые результаты пишет в JSONL, сводку с расходами рядом в <имя>.summary.json.
Решение и метрика без модели, поэтому токены нулевые и учет полный.

uv run python scripts/evaluate.py --candidate case/ways/baseline --data case/dataset/train.jsonl --out results/v2/baseline/train.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
METRIC = ROOT / "case" / "metric"
NO_MODEL = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "attempts": 0, "unreported_attempts": 0}


def digest(path: Path) -> str:
    """sha256 файла или каталога кандидата (пути и содержимое, без __pycache__)."""
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(str(f.relative_to(path)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def run_example(candidate: Path, example: dict) -> dict:
    task = {"input": example["input"], "uses_model": False, "assets_dir": str(ROOT)}
    env = {**os.environ, "MOUNTSIM_HOME": str(ROOT)}
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(candidate / "solve.py")], input=json.dumps(task), capture_output=True, text=True, env=env, timeout=900)
    solver_seconds = time.monotonic() - started
    if proc.returncode != 0:
        raise RuntimeError(f"solve.py упал на {example['id']}: {proc.stderr[-2000:]}")
    answer = json.loads(proc.stdout)["answer"]
    started = time.monotonic()
    scored = subprocess.run([sys.executable, str(METRIC / "score.py")], input=json.dumps({"example": example, "answer": answer}), capture_output=True, text=True, check=True)
    judge_seconds = time.monotonic() - started
    result = json.loads(scored.stdout)
    resources = {
        "solver": {"wall_seconds": solver_seconds, "usage": dict(NO_MODEL)},
        "judge": {"wall_seconds": judge_seconds, "usage": dict(NO_MODEL)},
    }
    return {"example_id": example["id"], "answer": answer, **result, "resources": resources}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--out", type=Path)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--quiet", action="store_true", help="печатать только итоговый score, как public")
    a = p.parse_args()
    examples = [json.loads(line) for line in a.data.read_text().splitlines() if line.strip()]
    if a.limit:
        examples = examples[: a.limit]
    started = time.monotonic()
    with ThreadPoolExecutor(a.jobs) as pool:
        results = list(pool.map(lambda ex: run_example(a.candidate.resolve(), ex), examples))
    agg = subprocess.run([sys.executable, str(METRIC / "aggregate.py")], input=json.dumps({"results": results}), capture_output=True, text=True, check=True)
    score = json.loads(agg.stdout)["score"]
    summary = {
        "score": score,
        "metric_version": json.loads((METRIC / "metric.json").read_text())["version"],
        "data": str(a.data.name),
        "data_sha256": digest(a.data),
        "examples": len(results),
        "candidate_sha256": digest(a.candidate.resolve()),
        "solver_tokens": 0,
        "solver_seconds": round(sum(r["resources"]["solver"]["wall_seconds"] for r in results), 3),
        "solver_unreported_attempts": 0,
        "judge_tokens": 0,
        "judge_seconds": round(sum(r["resources"]["judge"]["wall_seconds"] for r in results), 3),
        "judge_unreported_attempts": 0,
    }
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        with a.out.open("w") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        a.out.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
    if a.quiet:
        print(json.dumps({"score": score}))
        return
    groups: dict[str, list[float]] = defaultdict(list)
    invalid = []
    for r in results:
        s = r["statistics"]
        if not s["valid"]:
            invalid.append((r["example_id"], s.get("reason")))
            continue
        groups[f"kind={s['kind']}"].append(r["score"])
        groups[f"rtt={s['rtt_ms']} loss={s['loss']}"].append(r["score"])
    print(f"score {score:.4f}  examples {len(results)}  invalid {len(invalid)}  wall {time.monotonic() - started:.0f} s")
    for key in sorted(groups):
        vals = groups[key]
        print(f"  {key:22s} speedup x{2 ** (sum(vals) / len(vals)):.3f}  n={len(vals)}")
    for ex_id, reason in invalid[:10]:
        print(f"  INVALID {ex_id}: {reason}")


if __name__ == "__main__":
    main()
