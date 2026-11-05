"""Жульнические и сломанные кандидаты для тестов. Каждый это baseline плюс одна подмена."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "case" / "ways" / "baseline"

WRAP_REPLY = '''
_Base = Client


class Client(_Base):
    def __init__(self, env, config):
        super().__init__(env, config)
        self.keys = {}
        orig = env.reply

        def reply(op_id, result):
            orig(op_id, self.patch(self.keys.pop(op_id, None), result))

        env.reply = reply

    def on_op(self, op_id, op):
        self.keys[op_id] = op
        if self.intercept(op_id, op):
            return
        super().on_op(op_id, op)

    def intercept(self, op_id, op):
        return False

    def patch(self, op, result):
        return result
'''

CHEATERS = {
    # кэширует чтения навсегда и не узнает о правках пользователя
    "stale_forever": WRAP_REPLY + '''
    cache = {}

    def intercept(self, op_id, op):
        key = (op["op"], op.get("path"), op.get("offset"), op.get("size"))
        if op["op"] in ("read", "stat", "listdir") and key in self.cache:
            self.keys.pop(op_id, None)
            self.env.reply(op_id, self.cache[key])
            return True
        return False

    def patch(self, op, result):
        if op and op["op"] in ("read", "stat", "listdir"):
            self.cache[(op["op"], op.get("path"), op.get("offset"), op.get("size"))] = result
        return result
''',
    # подтверждает запись, но ничего не отправляет
    "lost_write": WRAP_REPLY + '''
    def intercept(self, op_id, op):
        if op["op"] == "write":
            self.keys.pop(op_id, None)
            self.env.reply(op_id, {"ok": True})
            return True
        return False
''',
    # портит прочитанные данные
    "garbage": WRAP_REPLY + '''
    def patch(self, op, result):
        data = result.get("data")
        if data:
            result = dict(result, data=bytes([data[0] ^ 1]) + data[1:])
        return result
''',
    # в режиме read-only все равно отвечает успехом на запись
    "ro_ignored": WRAP_REPLY + '''
    def intercept(self, op_id, op):
        if self.config["read_only"] and op["op"] in ("create", "write", "mkdir", "unlink", "rename", "truncate"):
            self.keys.pop(op_id, None)
            self.env.reply(op_id, {"ok": True})
            return True
        return False
''',
    # подменяет код ошибки при попытке выйти из папки
    "escape_enoent": WRAP_REPLY + '''
    def patch(self, op, result):
        if result.get("error") == "EACCES":
            result = dict(result, error="ENOENT")
        return result
''',
    # не отвечает на flush
    "hang": WRAP_REPLY + '''
    def intercept(self, op_id, op):
        return op["op"] == "flush"
''',
    # падает на листинге
    "crash": WRAP_REPLY + '''
    def intercept(self, op_id, op):
        if op["op"] == "listdir":
            raise RuntimeError("boom")
        return False
''',
    # тратит процессор на каждую операцию
    "slow_cpu": WRAP_REPLY + '''
    def intercept(self, op_id, op):
        import time
        end = time.process_time() + 0.004
        while time.process_time() < end:
            pass
        return False
''',
    # кэш на диске между экземплярами
    "disk_cache": WRAP_REPLY + '''
    import pickle as _pickle
    import tempfile as _tempfile
    _file = __import__("pathlib").Path(_tempfile.gettempdir()) / "ar02_cheat_cache.pkl"

    def intercept(self, op_id, op):
        cache = self._pickle.loads(self._file.read_bytes()) if self._file.exists() else {}
        key = (op["op"], op.get("path"), op.get("offset"), op.get("size"))
        if op["op"] == "read" and key in cache:
            self.keys.pop(op_id, None)
            self.env.reply(op_id, cache[key])
            return True
        return False

    def patch(self, op, result):
        if op and op["op"] == "read" and result.get("ok"):
            cache = self._pickle.loads(self._file.read_bytes()) if self._file.exists() else {}
            cache[(op["op"], op.get("path"), op.get("offset"), op.get("size"))] = result
            self._file.write_bytes(self._pickle.dumps(cache))
        return result
''',
}


def make(name: str, tmp: Path) -> Path:
    cand = tmp / name
    (cand / "protocol").mkdir(parents=True)
    (cand / "protocol" / "__init__.py").write_text("")
    (cand / "protocol" / "server.py").write_text((BASELINE / "protocol" / "server.py").read_text())
    (cand / "protocol" / "client.py").write_text((BASELINE / "protocol" / "client.py").read_text() + CHEATERS[name])
    return cand
