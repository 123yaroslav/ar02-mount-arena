"""Сервер протокола монтирования. Работает на машине пользователя рядом с папкой.

Baseline. Выполняет запрос клиента над папкой и отправляет ответ. Об изменениях
папки клиенту не сообщает, потому что у клиента нет кэша.

Интерфейс (его вызывает раннер, менять сигнатуры нельзя).
Server(env, fs, config)          env.now(), env.send(bytes), env.set_timer(delay, key)
                                 fs это папка пользователя: stat, listdir, read, create, write,
                                 truncate, mkdir, unlink, rename. Ошибка ФС приходит исключением
                                 с атрибутом code (ENOENT, EACCES, EROFS и другие).
on_start()                       сессия началась
on_message(data)                 пришло сообщение от клиента
on_local_change(path)            пользователь изменил path в своей папке (как inotify)
on_timer(key)                    сработал таймер
"""

import json
import struct


def pack(header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(h)) + h + payload


def unpack(data: bytes) -> tuple[dict, bytes]:
    n = struct.unpack(">I", data[:4])[0]
    return json.loads(data[4 : 4 + n]), data[4 + n :]


class Server:
    def __init__(self, env, fs, config):
        self.env = env
        self.fs = fs
        self.config = config

    def on_start(self):
        pass

    def on_message(self, data):
        h, payload = unpack(data)
        out = b""
        try:
            resp, out = self.execute(h, payload)
        except Exception as e:
            code = getattr(e, "code", None)
            if code is None:
                raise
            resp = {"ok": False, "error": code}
        resp["req"] = h["req"]
        self.env.send(pack(resp, out))

    def execute(self, h: dict, payload: bytes) -> tuple[dict, bytes]:
        fs, op, path = self.fs, h["op"], h.get("path")
        if op == "stat":
            st = fs.stat(path)
            return {"ok": True, "type": st["type"], "size": st["size"]}, b""
        if op == "listdir":
            return {"ok": True, "entries": fs.listdir(path)}, b""
        if op == "read":
            return {"ok": True, "has_data": True}, fs.read(path, h["offset"], h["size"])
        if op == "create":
            fs.create(path)
        elif op == "write":
            fs.write(path, h["offset"], payload)
        elif op == "truncate":
            fs.truncate(path, h["size"])
        elif op == "mkdir":
            fs.mkdir(path)
        elif op == "unlink":
            fs.unlink(path)
        elif op == "rename":
            fs.rename(path, h["dst"])
        elif op != "flush":
            raise ValueError(f"unknown op {op}")
        return {"ok": True}, b""

    def on_local_change(self, path):
        pass

    def on_timer(self, key):
        pass
