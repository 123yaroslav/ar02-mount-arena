"""Прогон одного экземпляра: дерево x сценарий x профиль сети.

Раннер держит виртуальные часы, очередь событий и модель сети. Клиент и сервер
кандидата живут в отдельных процессах и общаются только сообщениями через раннер.
Раннер сверяет каждый ответ клиента с эталоном и проверяет, что записи агента
доходят до папки пользователя за допустимое время.

Итоговое время экземпляра это сумма ожидания агентом всех операций ФС в виртуальном
времени плюс процессорное время клиента и сервера.
"""

from __future__ import annotations

import heapq
import itertools
import json
import multiprocessing as mp
import random
import time
from pathlib import Path

from . import worker
from .fs import digest
from .netmodel import Link, Profile
from .oracle import Oracle
from .scenario import build_scenario
from .tree import build_tree

VERSION = "mountsim-v2"
CALL_TIMEOUT = 10.0  # реальных секунд на один вызов кандидата
START_TIMEOUT = 60.0  # реальных секунд на запуск процессов
WALL_LIMIT = 300.0  # реальных секунд на экземпляр
OP_TIMEOUT = 120.0  # виртуальных секунд на одну операцию агента
MAX_EVENTS = 3_000_000
WATCH_DELAY = 0.05  # задержка уведомления об изменении файла (inotify, FSEvents), с


class Violation(Exception):
    pass


class CandidateFailure(Exception):
    pass


class Side:
    def __init__(self, ctx, side: str, candidate_dir: Path, config: dict, tree_spec: dict | None, clock_offset: float):
        self.side = side
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(
            target=worker.main, args=(child, side, str(candidate_dir), config, tree_spec, clock_offset), daemon=True
        )
        self.proc.start()
        child.close()

    def _recv(self, timeout: float):
        if not self.conn.poll(timeout):
            raise CandidateFailure(f"{self.side}: нет ответа за {timeout} с")
        try:
            return self.conn.recv()
        except EOFError as e:
            raise CandidateFailure(f"{self.side}: процесс завершился") from e

    def wait_ready(self) -> None:
        msg = self._recv(START_TIMEOUT)
        if msg[0] != "ready":
            raise CandidateFailure(f"{self.side}: запуск упал\n{msg[1]}")

    def request(self, *msg, timeout: float = CALL_TIMEOUT):
        self.conn.send(msg)
        resp = self._recv(timeout)
        if resp[0] == "error":
            raise CandidateFailure(f"{self.side}: {resp[1]}")
        return resp[1]

    def close(self) -> None:
        try:
            self.conn.send(("exit",))
        except Exception:
            pass
        self.proc.join(1.0)
        if self.proc.is_alive():
            self.proc.kill()
            self.proc.join(1.0)


def _short(state: tuple) -> tuple:
    if state and state[0] == "file":
        return ("file", digest(state[1]))
    return state


class Simulation:
    def __init__(self, sides: dict[str, Side], steps: list[dict], oracle: Oracle, profile: Profile, staleness: float, started: float):
        self.sides = sides
        self.steps = steps
        self.oracle = oracle
        self.T = staleness
        self.started = started
        self.now = 0.0
        self.heap: list = []
        self.seq = itertools.count()
        # по стороне-отправителю; джиттер из системного генератора, протокол его не предскажет
        noise = random.Random(random.SystemRandom().getrandbits(64))
        self.links = {"client": Link(profile, noise), "server": Link(profile, noise)}
        self.i = 0
        self.pending: tuple[int, dict, float] | None = None
        self.op_ids = itertools.count(1)
        self.by_op: dict[str, list[float]] = {}
        self.fs_wait = 0.0
        self.ops = 0
        self.done = False
        self.events = 0
        self.visibility: list[tuple[str, float, float, tuple]] = []

    def schedule(self, t: float, kind: str, *args) -> None:
        heapq.heappush(self.heap, (t, next(self.seq), kind, args))

    def call(self, side: str, method: str, *args) -> None:
        actions = self.sides[side].request("call", method, args, self.now)
        for act in actions:
            if act[0] == "send":
                other = "server" if side == "client" else "client"
                arrival = self.links[side].transmit(self.now, len(act[1]))
                self.schedule(arrival, "deliver", other, act[1])
            elif act[0] == "timer":
                self.schedule(self.now + act[1], "timer", side, act[2])
            elif act[0] == "reply":
                self.on_reply(act[1], act[2])

    def on_reply(self, op_id: int, result) -> None:
        if self.pending is None or self.pending[0] != op_id:
            raise Violation(f"t={self.now:.3f} ответ на неизвестную или закрытую операцию {op_id}")
        _, op, t0 = self.pending
        self.pending = None
        err, touched = self.oracle.check(op, result, t0, self.now)
        if err:
            raise Violation(f"t={self.now:.3f} {err}")
        wait = self.now - t0
        self.fs_wait += wait
        self.ops += 1
        stat = self.by_op.setdefault(op["op"], [0, 0.0])
        stat[0] += 1
        stat[1] += wait
        for path in touched:
            self.schedule(self.now + self.T, "visible", path, self.now)
        if op["op"] == "flush":
            self.done = True
        else:
            self.schedule(self.now, "step")

    def step(self) -> None:
        if self.i >= len(self.steps):
            return
        st = self.steps[self.i]
        self.i += 1
        if "think" in st:
            self.schedule(self.now + st["think"], "step")
        elif "user" in st:
            changed = self.sides["server"].request("edit", st["user"], self.now)
            self.oracle.user_edit(st["user"], self.now)
            for path in changed:
                self.schedule(self.now + WATCH_DELAY, "notify", path)
            self.schedule(self.now, "step")
        else:
            op = st["op"]
            op_id = next(self.op_ids)
            self.pending = (op_id, op, self.now)
            self.call("client", "on_op", op_id, dict(op))

    def check_visible(self, path: str, t_ack: float) -> None:
        state = self.sides["server"].request("state", [path])[0]
        self.visibility.append((path, t_ack, self.now, _short(state)))

    def verify_visibility(self) -> None:
        for path, t_ack, t_check, state in self.visibility:
            allowed = {_short(s) for s in self.oracle.acceptable_after(path, t_ack)}
            if state not in allowed:
                raise Violation(
                    f"t={t_check:.3f} запись агента в {path} (подтверждена в {t_ack:.3f}) не видна у пользователя через {self.T} с"
                )

    def run(self) -> None:
        self.call("server", "on_start")
        self.call("client", "on_start")
        self.schedule(0.0, "step")
        while self.heap and not self.done:
            t, _, kind, args = heapq.heappop(self.heap)
            if self.pending and t > self.pending[2] + OP_TIMEOUT:
                op = self.pending[1]
                raise Violation(f"операция {op['op']} {op.get('path', '')} без ответа дольше {OP_TIMEOUT} с")
            self.now = t
            self.events += 1
            if self.events > MAX_EVENTS:
                raise Violation("слишком много событий")
            if self.events % 2000 == 0 and time.monotonic() - self.started > WALL_LIMIT:
                raise Violation(f"экземпляр идет дольше {WALL_LIMIT} реальных секунд")
            if kind == "step":
                self.step()
            elif kind == "deliver":
                self.call(args[0], "on_message", args[1])
            elif kind == "timer":
                self.call(args[0], "on_timer", args[1])
            elif kind == "notify":
                self.call("server", "on_local_change", args[0])
            elif kind == "visible":
                self.check_visible(*args)
        if not self.done:
            op = self.pending[1] if self.pending else {"op": "?"}
            raise Violation(f"сценарий не завершился, операция {op['op']} {op.get('path', '')} ждет ответа, а событий больше нет")

    def final_check(self) -> dict[str, str]:
        self.verify_visibility()
        server = self.sides["server"].request("snapshot", timeout=60.0)
        expected = self.oracle.fs.snapshot()
        if server != expected:
            diff = sorted(p for p in set(server) | set(expected) if server.get(p) != expected.get(p))
            raise Violation(f"после flush папка пользователя расходится с ожидаемой в {len(diff)} путях, например {diff[:5]}")
        return server


def run_instance(spec: dict, candidate_dir: str | Path) -> dict:
    started = time.monotonic()
    profile = Profile.from_spec(spec["profile"])
    read_only = spec["mode"] == "ro"
    staleness = float(spec.get("staleness_seconds", 1.0))
    config = {"read_only": read_only, "staleness_seconds": staleness}
    report: dict = {"version": VERSION, "ok": False, "violations": []}
    ctx = mp.get_context("spawn")
    sides: dict[str, Side] = {}
    sim = None
    try:
        clocks = random.SystemRandom()
        sides["client"] = Side(ctx, "client", Path(candidate_dir), config, None, clocks.uniform(1e6, 2e6))
        sides["server"] = Side(ctx, "server", Path(candidate_dir), config, spec["tree"], clocks.uniform(1e6, 2e6))
        tree = build_tree(spec["tree"])
        steps = build_scenario(tree, spec["scenario"], read_only)
        oracle = Oracle(tree.fs, staleness, read_only)
        for side in sides.values():
            side.wait_ready()
        sim = Simulation(sides, steps, oracle, profile, staleness, started)
        sim.run()
        snapshot = sim.final_check()
        report["final_digest"] = digest(json.dumps(snapshot, sort_keys=True).encode())
        report["cpu_client"] = sides["client"].request("cpu")
        report["cpu_server"] = sides["server"].request("cpu")
        report["ok"] = True
    except Violation as v:
        report["violations"].append(str(v))
    except CandidateFailure as e:
        report["violations"].append("сбой кандидата: " + str(e)[-3000:])
    finally:
        for side in sides.values():
            side.close()
    if sim is not None:
        report["ops"] = sim.ops
        report["fs_wait"] = sim.fs_wait
        report["virtual_end"] = sim.now
        report["by_op"] = {k: {"count": v[0], "wait": v[1]} for k, v in sorted(sim.by_op.items())}
        report["traffic"] = {
            "to_server": {"messages": sim.links["client"].messages, "payload_bytes": sim.links["client"].payload_bytes, "wire_bytes": sim.links["client"].wire_bytes},
            "to_client": {"messages": sim.links["server"].messages, "payload_bytes": sim.links["server"].payload_bytes, "wire_bytes": sim.links["server"].wire_bytes},
        }
    if report["ok"]:
        report["total"] = report["fs_wait"] + report["cpu_client"] + report["cpu_server"]
    report["wall_seconds"] = time.monotonic() - started
    return report
