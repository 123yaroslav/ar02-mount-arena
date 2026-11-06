"""Train в репозитории совпадает с manifest и воспроизводится генератором из сида."""

import hashlib
import importlib.util
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "case" / "dataset"
MANIFEST = json.loads((DATA / "manifest.json").read_text())


def load_generator():
    spec = importlib.util.spec_from_file_location("generate", DATA / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def train_rows() -> list[dict]:
    return [json.loads(line) for line in (DATA / "train.jsonl").read_text().splitlines()]


def test_manifest_matches_train_file():
    train = MANIFEST["splits"]["train"]
    assert hashlib.sha256((DATA / train["file"]).read_bytes()).hexdigest() == train["sha256"]
    rows = train_rows()
    assert len(rows) == train["examples"]
    assert all(set(r) == {"id", "input", "reference"} for r in rows)
    assert {json.loads(r["reference"])["format"] for r in rows} == {MANIFEST["format"]}


def test_manifest_matches_generator_grid():
    gen = load_generator()
    grid = MANIFEST["grid"]
    assert gen.RTT_MS == grid["rtt_ms"] and gen.LOSS == grid["loss"]
    assert gen.BANDWIDTH_MBIT == grid["bandwidth_mbit"] and gen.STALENESS == grid["staleness_seconds"]
    assert gen.KINDS == grid["scenarios"] and sorted(gen.READ_ONLY_KINDS) == sorted(grid["read_only_scenarios"])
    per_tree = len(grid["scenarios"]) * len(grid["rtt_ms"]) * len(grid["loss"])
    for split in MANIFEST["splits"].values():
        assert split["examples"] == split["trees"] * per_tree


def test_train_inputs_regenerate_from_seed():
    gen = load_generator()
    train = MANIFEST["splits"]["train"]
    made = gen.make_examples("train", train["seed"], train["trees"])
    assert [(e["id"], e["input"]) for e in made] == [(r["id"], r["input"]) for r in train_rows()]


def test_tree_is_one_family():
    trees = defaultdict(set)
    for r in train_rows():
        spec = json.loads(r["input"])
        trees[r["id"].split("-")[1]].add(spec["tree"]["seed"])
    assert len(trees) == MANIFEST["splits"]["train"]["trees"]
    assert all(len(seeds) == 1 for seeds in trees.values())
    assert len({s for seeds in trees.values() for s in seeds}) == len(trees)


def test_other_split_seed_gives_other_trees():
    gen = load_generator()
    train = {json.loads(e["input"])["tree"]["seed"] for e in gen.make_examples("train", 1, 8)}
    other = {json.loads(e["input"])["tree"]["seed"] for e in gen.make_examples("public", 1, 4)}
    assert not train & other
