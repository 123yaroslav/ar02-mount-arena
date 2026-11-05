"""Классический протокол для сравнения: кэш с уведомлениями сервера и READDIRPLUS.

Идеи взяты из AFS (сервер сам сообщает клиенту об изменениях, клиент держит кэш до
уведомления) и NFSv3 (листинг каталога сразу возвращает атрибуты всех записей).
Файл до 1 МБ при первом чтении приходит целиком. Запись идет сразу на сервер,
агент ждет подтверждения. Этот вариант в эволюции не участвует, он служит ориентиром.
"""

import json
import struct

WHOLE_FILE = 1 << 20


def pack(header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(h)) + h + payload


def unpack(data: bytes) -> tuple[dict, bytes]:
    n = struct.unpack(">I", data[:4])[0]
    return json.loads(data[4 : 4 + n]), data[4 + n :]


def parent(path: str) -> str:
    path = "/".join(p for p in path.split("/") if p not in ("", "."))
    return path.rsplit("/", 1)[0] if "/" in path else ""


def norm(path: str) -> str:
    return "/".join(p for p in path.split("/") if p not in ("", "."))


class Client:
    def __init__(self, env, config):
        self.env = env
        self.ro = config["read_only"]
        self.next_req = 1
        self.waiting = {}
        self.attrs = {}  # путь -> ответ stat (или ошибка)
        self.listing = {}  # каталог -> список имен
        self.data = {}  # путь -> полное содержимое

    def on_start(self):
        pass

    def request(self, op_id, op, header, payload=b""):
        req = self.next_req
        self.next_req += 1
        self.waiting[req] = (op_id, op)
        header["req"] = req
        self.env.send(pack(header, payload))

    def on_op(self, op_id, op):
        kind = op["op"]
        path = norm(op.get("path", "")) if ".." not in op.get("path", "") else None
        if kind == "stat" and path is not None and path in self.attrs:
            self.env.reply(op_id, dict(self.attrs[path]))
            return
        if kind == "listdir" and path is not None and path in self.listing:
            self.env.reply(op_id, {"ok": True, "entries": list(self.listing[path])})
            return
        if kind == "read" and path is not None and path in self.data:
            off, size = op["offset"], op["size"]
            self.env.reply(op_id, {"ok": True, "data": self.data[path][off : off + size]})
            return
        if kind == "read" and path is not None:
            header = {"op": "readwhole", "path": op["path"], "offset": op["offset"], "size": op["size"], "max": WHOLE_FILE}
            self.request(op_id, op, header)
            return
        if kind == "listdir":
            self.request(op_id, op, {"op": "readdirplus", "path": op["path"]})
            return
        header = {k: v for k, v in op.items() if k != "data"}
        self.request(op_id, op, header, op.get("data", b""))

    def invalidate(self, path):
        path = norm(path)
        self.attrs.pop(path, None)
        self.data.pop(path, None)
        self.listing.pop(path, None)
        par = parent(path)
        self.listing.pop(par, None)
        self.attrs.pop(par, None)

    def on_message(self, data):
        h, payload = unpack(data)
        if "inval" in h:
            for p in h["inval"]:
                self.invalidate(p)
            return
        op_id, op = self.waiting.pop(h.pop("req"))
        kind = op["op"]
        path = norm(op.get("path", ""))
        if kind == "listdir" and h.get("ok"):
            self.listing[path] = h["entries"]
            for name, attr in zip(h["entries"], h["attrs"]):
                self.attrs[f"{path}/{name}" if path else name] = attr
            self.env.reply(op_id, {"ok": True, "entries": h["entries"]})
            return
        if kind == "read" and h.get("ok"):
            if h.get("whole"):
                self.data[path] = payload
                off, size = op["offset"], op["size"]
                self.env.reply(op_id, {"ok": True, "data": payload[off : off + size]})
            else:
                self.env.reply(op_id, {"ok": True, "data": payload})
            return
        if kind == "stat":
            self.attrs[path] = {k: v for k, v in h.items()}
            self.env.reply(op_id, h)
            return
        if kind == "read":
            self.env.reply(op_id, h)
            return
        # изменения: сбрасываем кэш затронутых путей, ответ сервера передаем агенту
        if kind in ("create", "write", "truncate", "mkdir", "unlink", "rename"):
            self.invalidate(path)
            if kind == "rename":
                self.invalidate(op["dst"])
        self.env.reply(op_id, h)

    def on_timer(self, key):
        pass
