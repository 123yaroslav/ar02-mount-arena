"""Тесты симулятора: сеть, фасад папки, генератор и детерминизм."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mountsim.fs import FSError, LocalFS, MemFS  # noqa: E402
from mountsim.netmodel import Link, Profile  # noqa: E402
from mountsim.scenario import KINDS, build_scenario  # noqa: E402
from mountsim.tree import build_tree, describe  # noqa: E402


def test_small_message_costs_half_rtt():
    link = Link(Profile(rtt=0.08, loss=0.0, bandwidth=50e6 / 8))
    arrival = link.transmit(0.0, 100)
    assert 0.04 < arrival < 0.0402


def test_big_message_is_bandwidth_bound_and_fifo():
    link = Link(Profile(rtt=0.02, loss=0.0, bandwidth=50e6 / 8))
    first = link.transmit(0.0, 6_250_000)
    second = link.transmit(0.0, 100)
    assert first > 1.0 and second >= first


def test_loss_adds_rto_and_caps_bandwidth():
    lossy = Profile(rtt=0.2, loss=0.01, bandwidth=50e6 / 8)
    assert lossy.effective_bandwidth < 100_000
    clean = Link(Profile(rtt=0.2, loss=0.0, bandwidth=50e6 / 8)).transmit(0.0, 100)
    assert Link(lossy).transmit(0.0, 100) > clean


def test_facade_blocks_escape_and_read_only():
    fs = MemFS()
    fs.mkdir("src", 0.0)
    fs.add_link("src/out_link", "../../.ssh/id_rsa")
    local = LocalFS(fs, read_only=True, clock=lambda: 0.0)
    for path in ["../x", "/etc/passwd", "src/../../x", "src/out_link", "src/out_link/a"]:
        with pytest.raises(FSError) as e:
            local.stat(path)
        assert e.value.code == "EACCES"
    with pytest.raises(FSError) as e:
        local.create("src/new.txt")
    assert e.value.code == "EROFS"


def test_tree_is_deterministic():
    a = build_tree({"seed": 6, "content_seed": 1}).fs.snapshot()
    b = build_tree({"seed": 6, "content_seed": 1}).fs.snapshot()
    c = build_tree({"seed": 6, "content_seed": 2}).fs.snapshot()
    assert a == b
    assert set(a) == set(c) and a != c


def test_tree_shape_matches_stats_roughly():
    shapes = [describe(s) for s in range(30)]
    ratio = sum(d["dirs"] for d in shapes) / sum(d["files"] for d in shapes)
    assert 0.08 < ratio < 0.4
    assert all(60 <= d["files"] <= 2001 for d in shapes)


@pytest.mark.parametrize("kind", KINDS)
def test_scenarios_end_with_flush_and_probe_escape(kind):
    tree = build_tree({"seed": 6, "content_seed": 1})
    steps = build_scenario(tree, {"kind": kind, "seed": 3}, read_only=kind in ("walk", "grep"))
    ops = [s["op"] for s in steps if "op" in s]
    assert ops[-1]["op"] == "flush"
    assert any(o.get("path", "").startswith("../") for o in ops)
    assert all(math.isfinite(s["think"]) and s["think"] > 0 for s in steps if "think" in s)
