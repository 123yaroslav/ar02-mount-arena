"""Клиент протокола монтирования. Работает в сандбоксе рядом с агентом.

r1-c1. Реплика метаданных, кэш содержимого по блокам и аренда свежести.

Клиент держит копию всего дерева (имена, типы, размеры) и блоки содержимого,
которые прислал сервер. stat, listdir, отсутствующие имена, выход за корень,
EROFS и flush отвечаются локально без сети. read отвечается из кэша, если все
нужные блоки есть, иначе клиент просит недостающие.

Свежесть. Каждое сообщение сервера несет его время ts, канал FIFO. Получив ts,
клиент знает обо всех правках пользователя, которые сервер увидел к ts, а сервер
видит правку не позже чем через 0,05 с. Поэтому ответ из кэша на операцию,
начатую в t0, допустим, если последняя полученная метка не старше t0 - (T - LEASE_MARGIN).
Иначе клиент сначала синхронизируется с сервером. Записи агента идут на сервер
сразу (write-through), подтвержденная запись применяется к кэшу и сверяется по crc.

Интерфейс (его вызывает раннер, менять сигнатуры нельзя).
Client(env, config)              env.now(), env.send(bytes), env.set_timer(delay, key), env.reply(op_id, result)
on_start()                       сессия началась, агент еще думает над первым шагом
on_op(op_id, op)                 операция агента, ответить env.reply(op_id, result) сейчас или позже
on_message(data)                 пришло сообщение от сервера
on_timer(key)                    сработал таймер
"""

import json
import struct
import zlib
from array import array

B = 65536
LARGE = 1536 * 1024
LEASE_MARGIN = 0.2
MAX_TRIES = 4  # после стольких запросов операция чтения уходит на сервер целиком, как в baseline
MUTATIONS = {"create", "write", "truncate", "mkdir", "unlink", "rename"}


def pack(header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(h)) + h + payload


def unpack(data: bytes) -> tuple[dict, bytes]:
    n = struct.unpack(">I", data[:4])[0]
    return json.loads(data[4 : 4 + n]), data[4 + n :]


def norm(path):
    if not isinstance(path, str) or path.startswith("/") or "\x00" in path:
        return None
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def wire(n: int) -> int:
    m = n + 8
    return m + -(-m // 1448) * 66


def err(code: str) -> dict:
    return {"ok": False, "error": code}


class Client:
    def __init__(self, env, config):
        self.env = env
        self.ro = bool(config.get("read_only"))
        self.L = float(config.get("staleness_seconds", 1.0)) - LEASE_MARGIN
        self.meta = None  # путь -> ["d", имена] | ["f", размер] | ["e", код]
        self.blocks = {}  # путь -> {номер блока: bytes}
        self.lease = -1e18
        self.pending = None  # (op_id, op, t0)
        self.dep = None  # номер запроса, от которого зависит текущая операция
        self.force_next = False
        self.asked = {}  # (путь, блок) -> номер запроса, который его уже просит
        self.req_keys = {}  # номер запроса -> ключи asked
        self.next_req = 1
        self.tries = 0
        self.direct = None
        self.dec = {}
        self.probe_t = {}
        self.meta_at = None
        self.lines = []  # словарь строк, тот же, что у сервера

    # сеть
    def request(self, h: dict, payload: bytes = b"") -> int:
        req = self.next_req
        self.next_req += 1
        h["r"] = req
        self.dep = req
        self.env.send(pack(h, payload))
        return req

    def sync(self) -> None:
        if self.dep is None:
            if self.too_many():
                return
            self.request({"t": "s"})

    def too_many(self) -> bool:
        """Страховка от зацикливания: после MAX_TRIES запросов спросить сервер напрямую."""
        self.tries += 1
        if self.tries <= MAX_TRIES:
            return False
        op = self.pending[1]
        h = {k: v for k, v in op.items() if k != "data"}
        h["t"] = "d"
        self.direct = self.request(h)
        return True

    def on_start(self):
        pass

    # реплика
    def lookup(self, p: str):
        """Повторяет обход MemFS. Возвращает запись или строку с кодом ошибки."""
        meta = self.meta
        e = meta[""]
        if not p:
            return e
        cur = ""
        for part in p.split("/"):
            k = e[0]
            if k == "e":
                return e[1]
            if k != "d":
                return "ENOTDIR"
            cur = cur + "/" + part if cur else part
            e = meta.get(cur)
            if e is None:
                return "ENOENT"
        if e[0] == "e":
            return e[1]
        return e

    def drop_meta(self, p: str) -> None:
        old = self.meta.pop(p, None)
        self.blocks.pop(p, None)
        if old is not None and old[0] == "d":
            for n in old[1]:
                self.drop_meta(p + "/" + n if p else n)

    def apply_meta(self, changes: dict) -> None:
        meta = self.meta
        for p, e in changes.items():
            old = meta.get(p)
            if e is None:
                if old is not None:
                    self.drop_meta(p)
                continue
            if old is not None and old[0] == "d" and e[0] != "d":
                self.drop_meta(p)
            elif old is not None and old[0] == "f" and (e[0] != "f" or e[1] != old[1]):
                self.blocks.pop(p, None)
            meta[p] = e

    def assemble(self, p: str, size: int):
        n = -(-size // B)
        bl = self.blocks.get(p)
        if n == 0:
            return b""
        if bl is None or len(bl) < n:
            return None
        try:
            return b"".join(bl[i] for i in range(n))
        except KeyError:
            return None

    def store(self, p: str, data: bytes) -> None:
        if not data:
            self.blocks.pop(p, None)
            return
        self.blocks[p] = {i: data[i * B : (i + 1) * B] for i in range(-(-len(data) // B))}

    # операции агента
    def on_op(self, op_id, op):
        self.pending = (op_id, op, self.env.now())
        self.force_next = False
        self.tries = 0
        self.direct = None
        self.serve()

    def finish(self, res: dict) -> None:
        op_id = self.pending[0]
        self.pending = None
        self.dep = None
        self.env.reply(op_id, res)

    def serve(self) -> None:
        if self.pending is None:
            return
        op_id, op, t0 = self.pending
        kind = op["op"]
        if self.direct is not None or (kind in MUTATIONS and self.dep is not None):
            return
        if kind == "flush":
            return self.finish({"ok": True})
        if kind in MUTATIONS:
            p = norm(op["path"])
            if p is None or (kind == "rename" and norm(op["dst"]) is None):
                return self.finish(err("EACCES"))
            if self.ro:
                return self.finish(err("EROFS"))
            h = {k: v for k, v in op.items() if k != "data"}
            h["t"] = "w"
            self.request(h, op.get("data", b""))
            return
        p = norm(op["path"])
        if p is None:
            return self.finish(err("EACCES"))
        if self.meta is None:
            return  # реплика еще в пути, она придет
        fresh = self.lease >= t0 - self.L
        e = self.lookup(p)
        if isinstance(e, str):
            if fresh:
                return self.finish(err(e))
            return self.sync()
        if kind == "stat":
            if not fresh:
                return self.sync()
            if e[0] == "d":
                return self.finish({"ok": True, "type": "dir", "size": 0})
            return self.finish({"ok": True, "type": "file", "size": e[1]})
        if kind == "listdir":
            if not fresh:
                return self.sync()
            if e[0] != "d":
                return self.finish(err("ENOTDIR"))
            return self.finish({"ok": True, "entries": list(e[1])})
        if kind != "read":
            return self.finish(err("EINVAL"))
        if e[0] == "d":
            if not fresh:
                return self.sync()
            return self.finish(err("EISDIR"))
        off, size = int(op["offset"]), int(op["size"])
        if off < 0 or size < 0:
            if not fresh:
                return self.sync()
            return self.finish(err("EINVAL"))
        fsize = e[1]
        end = min(off + size, fsize)
        if off >= end:
            if not fresh:
                return self.sync()
            return self.finish({"ok": True, "data": b""})
        b0 = off // B
        b1 = (end - 1) // B
        bl = self.blocks.get(p)
        missing = []
        if bl is None:
            missing = list(range(b0, b1 + 1))
        else:
            missing = [i for i in range(b0, b1 + 1) if i not in bl]
        if not missing:
            if not fresh:
                return self.sync()
            if b0 == b1:
                d = bl[b0]
                base = b0 * B
                return self.finish({"ok": True, "data": d[off - base : end - base]})
            buf = b"".join(bl[i] for i in range(b0, b1 + 1))
            base = b0 * B
            return self.finish({"ok": True, "data": buf[off - base : end - base]})
        asked = self.asked
        if all((p, i) in asked for i in missing):
            # все нужные блоки уже запрошены, ждем данных или ответа
            if self.dep is None:
                self.dep = asked[(p, missing[0])]
            return
        if fsize <= LARGE:
            n = -(-fsize // B)
            missing = list(range(n)) if bl is None else [i for i in range(n) if i not in bl]
        need = [i for i in missing if (p, i) not in asked]
        if self.too_many():
            return
        h = {"t": "f", "p": p, "b": need}
        if self.force_next:
            h["force"] = 1
        req = self.request(h)
        keys = [(p, i) for i in need]
        for k in keys:
            asked[k] = req
        self.req_keys[req] = keys

    # сообщения сервера
    def on_message(self, data):
        h, payload = unpack(data)
        self.lease = h["ts"]
        t = h["t"]
        if t == "c":
            self.on_data(h, payload)
        elif t == "u":
            self.apply_meta(h["m"])
            for p in h["x"]:
                self.blocks.pop(p, None)
        elif t == "a":
            if self.pending is not None and self.dep in h["r"]:
                self.on_ack(h)
            return
        elif t == "o":
            if self.pending is not None and self.direct is not None and self.direct in h["r"]:
                res = h["res"]
                if h.get("d"):
                    res["data"] = payload
                return self.finish(res)
        elif t == "m":
            self.meta = json.loads(zlib.decompress(payload))
            self.meta_at = (h["ts"], len(data))
        elif t == "p":
            self.probe_t[h["i"]] = (self.env.now(), len(data), h["ts"])
            if len(self.probe_t) == 2:
                (a0, _, ts0), (a1, n, _) = self.probe_t[0], self.probe_t[1]
                dt = a1 - a0
                bw = wire(n) / dt if dt > 0 else 1e9
                # задержка в одну сторону: приход первой пробы минус передача реплики и самой пробы
                ow = a0 - ts0 - dt - (wire(self.meta_at[1]) / bw if self.meta_at else 0.0)
                self.env.send(pack({"t": "bw", "v": bw, "ow": max(0.0, ow)}))
        r = h.get("r")
        if r is not None:
            asked = self.asked
            for req in r:
                for k in self.req_keys.pop(req, ()):
                    if asked.get(k) == req:
                        del asked[k]
        if r is not None and self.dep is not None and self.dep in r:
            self.dep = None
            # все, что сервер отправил до ответа, уже пришло; если блоков нет, просим их силой
            self.force_next = True
        self.serve()

    def on_data(self, h: dict, payload: bytes) -> None:
        z = h["z"]
        if z:
            d = self.dec.get(z)
            if d is None:
                d = self.dec[z] = zlib.decompressobj()
            payload = d.decompress(payload)
        k = h.get("k")
        if k is not None:
            ni, nl = k
            idx = array("H")
            idx.frombytes(payload[:ni])
            lines = self.lines
            if nl:
                lines.extend(payload[ni:].split(b"\n"))
            payload = b"\n".join(map(lines.__getitem__, idx))
        meta = self.meta
        pos = 0
        for p, b, n in h["f"]:
            chunk = payload[pos : pos + n]
            pos += n
            e = meta.get(p) if meta is not None else None
            if e is None or e[0] != "f":
                continue
            expect = min(B, e[1] - b * B)
            if expect <= 0 or expect != n:
                self.blocks.pop(p, None)
                continue
            bl = self.blocks.get(p)
            if bl is None:
                bl = self.blocks[p] = {}
            bl[b] = chunk

    def on_ack(self, h: dict) -> None:
        op_id, op, _ = self.pending
        kind = op["op"]
        self.dep = None
        if not h["ok"]:
            return self.finish(err(h["error"]))
        p = norm(op["path"])
        meta = self.meta
        if meta is not None:
            old = meta.get(p)
            content = None
            if kind == "create":
                content = b""
            elif kind in ("write", "truncate") and old is not None and old[0] == "f":
                cur = self.assemble(p, old[1])
                if cur is not None:
                    if kind == "write":
                        buf = bytearray(cur)
                        off = int(op["offset"])
                        if off > len(buf):
                            buf.extend(b"\x00" * (off - len(buf)))
                        data = op["data"]
                        buf[off : off + len(data)] = data
                        content = bytes(buf)
                    else:
                        size = int(op["size"])
                        content = cur[:size] + b"\x00" * max(0, size - len(cur))
            moved = None
            if kind == "rename":
                moved = self.blocks.pop(p, None)
            self.apply_meta(h["m"])
            for q in h["x"]:
                self.blocks.pop(q, None)
            if kind == "rename":
                d = norm(op["dst"])
                self.blocks.pop(d, None)
                if moved is not None and d in meta and meta[d][0] == "f":
                    self.blocks[d] = moved
            if kind in ("create", "write", "truncate"):
                self.blocks.pop(p, None)
                if content is not None:
                    self.store(p, content)
            for q, (size, crc) in h["c"].items():
                if q not in self.blocks:
                    continue
                cur = self.assemble(q, size)
                if cur is None or len(cur) != size or zlib.crc32(cur) != crc:
                    self.blocks.pop(q, None)
        self.finish({"ok": True})

    def on_timer(self, key):
        pass
