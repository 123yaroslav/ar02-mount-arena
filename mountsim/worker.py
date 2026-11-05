"""Процесс одной стороны протокола: клиент в сандбоксе или сервер у пользователя.

Каждая сторона живет в отдельном свежем процессе (spawn). Клиент не видит папку
пользователя, сервер не видит сценарий агента. Код кандидата импортируется только здесь.
"""

from __future__ import annotations

import importlib
import os
import resource
import sys
import traceback

from .fs import FSError, LocalFS, apply_user_edit


def _cpu() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


class Env:
    """То, что протокол может делать с окружением. Время виртуальное."""

    def __init__(self, side: str, clock_offset: float = 0.0):
        self._side = side
        self._now = 0.0
        self._offset = clock_offset
        self._actions: list[tuple] = []

    def now(self) -> float:
        """Локальные часы стороны. У клиента и сервера разное неизвестное смещение."""
        return self._now + self._offset

    def send(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("send принимает только bytes")
        self._actions.append(("send", bytes(data)))

    def set_timer(self, delay: float, key=None) -> None:
        delay = float(delay)
        if not delay >= 0:
            raise ValueError("задержка таймера должна быть неотрицательной")
        self._actions.append(("timer", delay, key))

    def reply(self, op_id: int, result: dict) -> None:
        if self._side != "client":
            raise RuntimeError("reply доступен только клиенту")
        self._actions.append(("reply", op_id, result))


def main(conn, side: str, candidate_dir: str, config: dict, tree_spec: dict | None, clock_offset: float = 0.0) -> None:
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    sys.stdout = open(os.devnull, "w")
    try:
        env = Env(side, clock_offset)
        fs = None
        memfs = None
        if side == "server":
            from .tree import build_tree

            memfs = build_tree(tree_spec).fs  # type: ignore[arg-type]
            fs = LocalFS(memfs, read_only=config["read_only"], clock=env.now)
        sys.path.insert(0, candidate_dir)
        module = importlib.import_module(f"protocol.{side}")
        cpu_start = _cpu()
        if side == "client":
            obj = module.Client(env, dict(config))
        else:
            obj = module.Server(env, fs, dict(config))
        conn.send(("ready",))
    except BaseException:
        conn.send(("error", traceback.format_exc(limit=8)))
        return

    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        kind = msg[0]
        try:
            if kind == "call":
                _, method, args, now = msg
                env._now = now
                env._actions = []
                getattr(obj, method)(*args)
                conn.send(("done", env._actions))
            elif kind == "edit":
                _, edit, now = msg
                conn.send(("done", apply_user_edit(memfs, edit, now)))  # type: ignore[arg-type]
            elif kind == "state":
                conn.send(("done", [memfs.state(p) for p in msg[1]]))  # type: ignore[union-attr]
            elif kind == "snapshot":
                conn.send(("done", memfs.snapshot()))  # type: ignore[union-attr]
            elif kind == "cpu":
                conn.send(("done", _cpu() - cpu_start))
            elif kind == "exit":
                return
        except FSError as e:
            conn.send(("error", f"необработанная ошибка ФС {e.code} в {msg[1] if len(msg) > 1 else ''}"))
        except BaseException:
            conn.send(("error", traceback.format_exc(limit=8)))
