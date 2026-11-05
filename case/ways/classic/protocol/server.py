"""Сервер классического протокола: READDIRPLUS, файл целиком и уведомления об изменениях."""

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

    def on_start(self):
        pass

    def stat_entry(self, path):
        try:
            st = self.fs.stat(path)
            return {"ok": True, "type": st["type"], "size": st["size"]}
        except Exception as e:
            return {"ok": False, "error": e.code}

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

    def execute(self, h, payload):
        fs, op, path = self.fs, h["op"], h.get("path")
        if op == "readdirplus":
            names = fs.listdir(path)
            base = path.rstrip("/")
            attrs = [self.stat_entry(f"{base}/{n}" if base else n) for n in names]
            return {"ok": True, "entries": names, "attrs": attrs}, b""
        if op == "readwhole":
            st = fs.stat(path)
            if st["type"] == "file" and st["size"] <= h["max"]:
                return {"ok": True, "whole": True}, fs.read(path, 0, st["size"])
            return {"ok": True}, fs.read(path, h["offset"], h["size"])
        if op == "stat":
            st = fs.stat(path)
            return {"ok": True, "type": st["type"], "size": st["size"]}, b""
        if op == "listdir":
            return {"ok": True, "entries": fs.listdir(path)}, b""
        if op == "read":
            return {"ok": True}, fs.read(path, h["offset"], h["size"])
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
            raise ValueError(op)
        return {"ok": True}, b""

    def on_local_change(self, path):
        self.env.send(pack({"inval": [path]}))

    def on_timer(self, key):
        pass
