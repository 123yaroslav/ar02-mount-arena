"""Тесты метрики AR-02.

Первая часть проверяет score.py и aggregate.py на синтетических отчетах.
Вторая часть гоняет симулятор с жульническими кандидатами. Каждый должен получить
нарушение гейта, а честный улучшенный протокол должен пройти и стать быстрее.
"""

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from cheaters import make

from mountsim.runner import run_instance

ROOT = Path(__file__).resolve().parents[3]
METRIC = ROOT / "case" / "metric"
BASELINE = ROOT / "case" / "ways" / "baseline"
CLASSIC = ROOT / "case" / "ways" / "classic"
SMALL_TREE = 6  # 109 файлов, тесты идут секунды


def spec(kind: str, rtt: int = 20, loss: float = 0.0, content_seed: int = 1, seed: int = 7) -> dict:
    return {
        "format": "ar02-v2",
        "tree": {"seed": SMALL_TREE, "content_seed": content_seed},
        "scenario": {"kind": kind, "seed": seed},
        "profile": {"rtt_ms": rtt, "loss": loss, "bandwidth_mbit": 50},
        "mode": "ro" if kind in ("walk", "grep") else "rw",
        "staleness_seconds": 1.0,
    }


def example_for(s: dict) -> dict:
    report = run_instance(s, BASELINE)
    assert report["ok"], report["violations"]
    ref = {"baseline_total": report["total"], "ops": report["ops"], "final_digest": report["final_digest"], "format": "ar02-v2"}
    return {"id": "t", "input": json.dumps(s), "reference": json.dumps(ref)}


def score(example: dict, answer: str) -> dict:
    out = subprocess.run([sys.executable, str(METRIC / "score.py")], input=json.dumps({"example": example, "answer": answer}), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def aggregate(results: list[dict]) -> float:
    out = subprocess.run([sys.executable, str(METRIC / "aggregate.py")], input=json.dumps({"results": results}), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["score"]


@pytest.fixture(scope="module")
def walk_example():
    return example_for(spec("walk"))


# score.py и aggregate.py на синтетических отчетах


def fake_report(example: dict, total: float, **over) -> str:
    ref = json.loads(example["reference"])
    rep = {"version": "mountsim-v2", "ok": True, "violations": [], "total": total, "ops": ref["ops"], "final_digest": ref["final_digest"], "cpu_client": 0.0, "cpu_server": 0.0}
    rep.update(over)
    return json.dumps(rep)


def test_twice_faster_scores_one(walk_example):
    base = json.loads(walk_example["reference"])["baseline_total"]
    r = score(walk_example, fake_report(walk_example, base / 2))
    assert r["statistics"]["valid"] and math.isclose(r["score"], 1.0)


def test_same_as_baseline_scores_zero(walk_example):
    base = json.loads(walk_example["reference"])["baseline_total"]
    assert math.isclose(score(walk_example, fake_report(walk_example, base))["score"], 0.0)


def test_violation_is_invalid(walk_example):
    r = score(walk_example, fake_report(walk_example, 0.01, ok=False, violations=["stale read"]))
    assert not r["statistics"]["valid"] and r["score"] < 0


def test_wrong_format_is_invalid(walk_example):
    assert not score(walk_example, "not json")["statistics"]["valid"]
    assert not score(walk_example, json.dumps({"total": 0.1}))["statistics"]["valid"]


def test_skipped_ops_are_invalid(walk_example):
    ops = json.loads(walk_example["reference"])["ops"]
    assert not score(walk_example, fake_report(walk_example, 0.1, ops=ops - 5))["statistics"]["valid"]


def test_report_from_other_example_is_invalid(walk_example):
    """Подгонка под известный экземпляр: чужой быстрый отчет не подходит к другому дереву."""
    other = example_for(spec("walk", content_seed=2))
    stolen = fake_report(other, 0.001)
    assert not score(walk_example, stolen)["statistics"]["valid"]


def test_aggregate_geometric_mean_and_zero_on_any_invalid():
    ok = {"score": 1.0, "statistics": {"valid": True}}
    ok2 = {"score": 3.0, "statistics": {"valid": True}}
    bad = {"score": -10.0, "statistics": {"valid": False}}
    assert math.isclose(aggregate([ok, ok2]), 4.0)
    assert aggregate([ok, ok2, bad]) == 0.0


# симулятор и жульнические кандидаты


@pytest.mark.parametrize(
    "name,kind",
    [
        ("stale_forever", "session"),
        ("lost_write", "writes"),
        ("garbage", "small_reads"),
        ("ro_ignored", "walk"),
        ("escape_enoent", "walk"),
        ("hang", "walk"),
        ("crash", "walk"),
    ],
)
def test_cheater_is_caught(name, kind):
    with tempfile.TemporaryDirectory() as tmp:
        report = run_instance(spec(kind), make(name, Path(tmp)))
    assert not report["ok"] and report["violations"], report


def test_cache_between_instances_is_caught():
    """Одинаковое дерево, другое содержимое. Кэш с прошлого экземпляра выдает старые байты."""
    cache = Path(tempfile.gettempdir()) / "ar02_cheat_cache.pkl"
    cache.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        cand = make("disk_cache", Path(tmp))
        first = run_instance(spec("grep", content_seed=1), cand)
        second = run_instance(spec("grep", content_seed=2), cand)
    cache.unlink(missing_ok=True)
    assert first["ok"]
    assert not second["ok"]


def test_cpu_is_counted():
    s = spec("walk", rtt=1)
    base = run_instance(s, BASELINE)
    with tempfile.TemporaryDirectory() as tmp:
        slow = run_instance(s, make("slow_cpu", Path(tmp)))
    assert slow["ok"] and slow["total"] > base["total"] + 0.1


@pytest.mark.parametrize("kind", ["walk", "grep", "small_reads", "big_file", "writes", "session"])
@pytest.mark.parametrize("rtt,loss", [(1, 0.0), (200, 0.01)])
def test_honest_protocols_pass(kind, rtt, loss):
    s = spec(kind, rtt=rtt, loss=loss)
    base = run_instance(s, BASELINE)
    classic = run_instance(s, CLASSIC)
    assert base["ok"], base["violations"]
    assert classic["ok"], classic["violations"]
    assert base["final_digest"] == classic["final_digest"]


def test_better_protocol_scores_higher(walk_example):
    report = run_instance(json.loads(walk_example["input"]), CLASSIC)
    assert score(walk_example, json.dumps(report))["score"] > 0.5


def test_solve_py_end_to_end(walk_example):
    import os

    task = {"input": walk_example["input"], "uses_model": False}
    env = {**os.environ, "MOUNTSIM_HOME": str(ROOT)}
    out = subprocess.run([sys.executable, str(BASELINE / "solve.py")], input=json.dumps(task), capture_output=True, text=True, env=env, check=True)
    answer = json.loads(out.stdout)["answer"]
    r = score(walk_example, answer)
    assert r["statistics"]["valid"] and abs(r["score"]) < 0.3
