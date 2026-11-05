"""Клиент протокола монтирования. Работает в сандбоксе рядом с агентом.

Baseline. Каждая операция агента превращается в один запрос к серверу и ждет ответа.
Кэша нет, предвыборки нет, запись идет сразу на сервер.

Интерфейс (его вызывает раннер, менять сигнатуры нельзя).
Client(env, config)              env.now(), env.send(bytes), env.set_timer(delay, key), env.reply(op_id, result)
on_start()                       сессия началась, агент еще думает над первым шагом
on_op(op_id, op)                 операция агента, ответить env.reply(op_id, result) сейчас или позже
on_message(data)                 пришло сообщение от сервера
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


class Client:
    def __init__(self, env, config):
        self.env = env
        self.config = config
        self.next_req = 1
        self.waiting = {}

    def on_start(self):
        pass

    def on_op(self, op_id, op):
        req = self.next_req
        self.next_req += 1
        self.waiting[req] = op_id
        header = {k: v for k, v in op.items() if k != "data"}
        header["req"] = req
        self.env.send(pack(header, op.get("data", b"")))

    def on_message(self, data):
        header, payload = unpack(data)
        op_id = self.waiting.pop(header.pop("req"))
        if header.pop("has_data", False):
            header["data"] = payload
        self.env.reply(op_id, header)

    def on_timer(self, key):
        pass
