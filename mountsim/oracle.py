"""Эталон для проверки ответов протокола.

Эталон хранит логическое состояние папки. Запись агента вступает в силу в момент,
когда клиент подтвердил операцию. Правка пользователя вступает в силу в момент
применения. Для каждого измененного пути хранится история версий.

Правило свежести. Ответ на чтение пути p, начатое в t0 и законченное в t1, должен
совпадать с какой-то версией p, действовавшей в окне от max(t0 - T, последняя запись
агента в p) до t1. Здесь T это допустимое запаздывание из условия кейса.
"""

from __future__ import annotations

import math

from .fs import FSError, MemFS, split_path

MUTATIONS = {"create", "write", "truncate", "mkdir", "unlink", "rename"}
READS = {"stat", "listdir", "read"}


def norm(path: str) -> str | None:
    try:
        return "/".join(split_path(path))
    except FSError:
        return None


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


class Oracle:
    def __init__(self, fs: MemFS, staleness: float, read_only: bool):
        self.fs = fs
        self.T = staleness
        self.ro = read_only
        # путь -> список (время, состояние, запись ли агента)
        self.hist: dict[str, list[tuple[float, tuple, bool]]] = {}

    # история
    def _touch(self, paths: list[str], fn, now: float, by_agent: bool):
        before = {p: self.fs.state(p) for p in paths}
        fn()
        for p in paths:
            after = self.fs.state(p)
            if after != before[p]:
                h = self.hist.setdefault(p, [(-math.inf, before[p], False)])
                h.append((now, after, by_agent))

    def versions(self, path: str) -> list[tuple[float, tuple, bool]]:
        return self.hist.get(path) or [(-math.inf, self.fs.state(path), False)]

    def window_states(self, path: str, t0: float, t1: float) -> list[tuple]:
        h = self.versions(path)
        lo = t0 - self.T
        for eff, _, by_agent in h:
            if by_agent and eff <= t0:
                lo = max(lo, eff)
        out = []
        for i, (eff, state, _) in enumerate(h):
            end = h[i + 1][0] if i + 1 < len(h) else math.inf
            if eff <= t1 and end > lo:
                out.append(state)
        return out

    # правки пользователя
    def user_edit(self, edit: dict, now: float) -> None:
        from .fs import apply_user_edit

        path = norm(edit["path"])
        assert path is not None
        self._touch([path, _parent(path)], lambda: apply_user_edit(self.fs, edit, now), now, by_agent=False)

    # проверка ответа клиента
    def check(self, op: dict, result, t0: float, t1: float) -> tuple[str | None, list[str]]:
        """Возвращает (описание нарушения или None, пути для проверки видимости у пользователя)."""
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            return f"{op['op']} {op.get('path')}: ответ не словарь с полем ok", []
        kind = op["op"]
        if kind == "flush":
            return (None if result["ok"] else "flush: ok=False"), []
        if kind in READS:
            return self._check_read(op, result, t0, t1), []
        if kind in MUTATIONS:
            return self._check_mutation(op, result, t1)
        return f"неизвестная операция {kind}", []

    def _check_read(self, op: dict, result: dict, t0: float, t1: float) -> str | None:
        path = norm(op["path"])
        if path is None:
            return self._expect_error(op, result, "EACCES")
        states = self.window_states(path, t0, t1)
        for st in states:
            if self._matches(op, result, st):
                return None
        got = {k: (v if k != "data" else f"<{len(v)} байт>") for k, v in result.items()}
        return f"{op['op']} {op['path']}: ответ {got} не совпал ни с одной версией в окне свежести"

    @staticmethod
    def _expect_error(op: dict, result: dict, code: str) -> str | None:
        if result.get("ok") is False and result.get("error") == code:
            return None
        return f"{op['op']} {op.get('path')}: ожидалась ошибка {code}, получено {result.get('error') if not result.get('ok') else 'ok'}"

    @staticmethod
    def _matches(op: dict, result: dict, st: tuple) -> bool:
        kind = op["op"]
        tag = st[0]
        if tag == "absent":
            return result.get("ok") is False and result.get("error") == "ENOENT"
        if tag == "error":
            return result.get("ok") is False and result.get("error") == st[1]
        if kind == "stat":
            if not result.get("ok"):
                return False
            if tag == "dir":
                return result.get("type") == "dir"
            return result.get("type") == "file" and result.get("size") == len(st[1])
        if kind == "listdir":
            if tag == "file":
                return result.get("ok") is False and result.get("error") == "ENOTDIR"
            return result.get("ok") is True and list(result.get("entries", [])) == list(st[1])
        if kind == "read":
            if tag == "dir":
                return result.get("ok") is False and result.get("error") == "EISDIR"
            data = result.get("data")
            if not result.get("ok") or not isinstance(data, (bytes, bytearray)):
                return False
            off, size = int(op["offset"]), int(op["size"])
            return bytes(data) == st[1][off : off + size]
        return False

    def _check_mutation(self, op: dict, result: dict, now: float) -> tuple[str | None, list[str]]:
        kind = op["op"]
        path = norm(op["path"])
        dst = norm(op["dst"]) if kind == "rename" else None
        if path is None or (kind == "rename" and dst is None):
            return self._expect_error(op, result, "EACCES"), []
        if self.ro:
            return self._expect_error(op, result, "EROFS"), []
        fs = self.fs
        apply = {
            "create": lambda: fs.create(path, now),
            "write": lambda: fs.write(path, int(op["offset"]), bytes(op["data"]), now),
            "truncate": lambda: fs.truncate(path, int(op["size"]), now),
            "mkdir": lambda: fs.mkdir(path, now),
            "unlink": lambda: fs.unlink(path, now),
            "rename": lambda: fs.rename(path, dst, now),
        }[kind]
        touched = [path, _parent(path)] + ([dst, _parent(dst)] if dst else [])
        touched = list(dict.fromkeys(touched))
        # ожидаемый исход считаем на копии состояния путей, чтобы не применять дважды
        try:
            self._touch(touched, apply, now, by_agent=True)
            expected = None
        except FSError as e:
            expected = e.code
        if expected is None:
            if result.get("ok") is True:
                return None, [p for p in touched if p is not None]
            return f"{kind} {op['path']}: ожидался успех, получено {result.get('error')}", []
        return self._expect_error(op, result, expected), []

    # видимость записи агента у пользователя
    def acceptable_after(self, path: str, t_ack: float) -> list[tuple]:
        """Версии path, которые не старше подтвержденной записи агента.

        Сервер может применить следующую запись агента раньше, чем клиент ее подтвердит,
        поэтому годится любая более поздняя версия. Проверка идет в конце прогона,
        когда вся история уже известна.
        """
        h = self.versions(path)
        out = []
        for i, (eff, state, _) in enumerate(h):
            end = h[i + 1][0] if i + 1 < len(h) else math.inf
            if end > t_ack:
                out.append(state)
        return out
