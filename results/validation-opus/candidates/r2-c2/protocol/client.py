"""Клиент протокола монтирования. Работает в сандбоксе рядом с агентом.

r2-c2. Как r2-c1, плюс пачка stat и данных и чтение с опережением.

Пачка stat и данных. Если после stat файла агент обычно читает этот файл, клиент просит
сервер положить в ответ на stat все содержимое файла до 128 КБ. Чтение потом отвечается
из кэша. Любой ответ сервера несет время, когда сервер его прочитал (ts). Данные из кэша
годятся для операции, начатой до ts + T, это прямо следует из правила свежести. Свои
мутации стирают кэш пути, а ответы, прочитанные сервером до прихода своей мутации,
в кэш не попадают.

Чтение с опережением. Когда агент читает файл подряд кусками, клиент держит K запросов
на следующие куски в полете. K выбран так, чтобы канал был занят и чтобы каждый кусок
приходил еще свежим. Если кусок все же устарел, клиент читает его заново.

Как в r2-c1. Write-back с гарантией доставки за T, сжатие и компактная рамка.

Идея. Мутацию агента, исход которой клиент знает наверняка, клиент подтверждает сам
и отправляет на сервер без ожидания ответа. Чтобы запись дошла до пользователя за T,
клиент держит у себя точную копию модели своего канала (полоса, половина RTT, штрафы
за потери), откалиброванную пробами в начале сессии, пока агент думает. Ответ агенту
уходит не раньше, чем предсказанное время прихода минус T плюс запас. Исход мутации
клиент предсказывает по знанию о дереве: снимок пространства имен с сервера, ответы
на stat и listdir, собственные мутации и уведомления сервера о правках пользователя.
Если исход не известен наверняка, мутация идет синхронно, как в baseline.

Чтение идет на сервер, как в baseline, но данные сжимаются zlib, когда это выгодно.
Выход за папку и EROFS в режиме только для чтения клиент отвечает сам.

Интерфейс (его вызывает раннер, менять сигнатуры нельзя).
Client(env, config)              env.now(), env.send(bytes), env.set_timer(delay, key), env.reply(op_id, result)
on_start()                       сессия началась, агент еще думает над первым шагом
on_op(op_id, op)                 операция агента, ответить env.reply(op_id, result) сейчас или позже
on_message(data)                 пришло сообщение от сервера
on_timer(key)                    сработал таймер
"""

import json
import math
import struct
import zlib

# типы сообщений клиент -> сервер
OPS = ["stat", "listdir", "read", "create", "write", "truncate", "mkdir", "unlink", "rename", "flush"]
OP_CODE = {name: i + 1 for i, name in enumerate(OPS)}
M_PROBE = 20
M_NSREQ = 21
M_PARAMS = 22
F_ASYNC = 1
F_ZIP = 2
F_WANT = 4  # stat: положить содержимое файла в ответ
F_DATA = 8  # ответ stat несет содержимое файла
F_EOF = 16  # в ответе stat весь файл
FRESH = 0.05  # запас по свежести кэша, с
BIG_READ = 128 * 1024
PF_MAX = 48

# типы сообщений сервер -> клиент
R_RESULT = 1
R_PROBE = 2
R_NOTIFY = 3
R_NS = 4

ERRS = ["", "ENOENT", "ENOTDIR", "EISDIR", "EEXIST", "EROFS", "EACCES", "ENOTEMPTY", "EINVAL"]
TYPES = {1: "file", 2: "dir"}
MUTATIONS = {"create", "write", "truncate", "mkdir", "unlink", "rename"}

# константы модели сети, те же, что у TCP: сегмент, заголовки пакета, рамка сообщения
MSS = 1448
PKT = 66
FRAME = 8

# пробы для калибровки: размер сообщения подобран под 1, 1, 3 и 12 пакетов
PROBE_SIZES = [24, 1400, 4200, 17318]
MARGIN = 0.03  # запас по времени доставки, с
ZIP_MIN = 512

HDR = struct.Struct(">BBI")
RHDR = struct.Struct(">BBBId")


def norm(path):
    """Нормализация пути как у фасада. None значит выход за папку (EACCES)."""
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


def parent_of(p):
    i = p.rfind("/")
    return p[:i] if i >= 0 else ""


def name_of(p):
    return p[p.rfind("/") + 1 :]


def enc_path(p):
    b = p.encode()
    return struct.pack(">H", len(b)) + b


class LinkModel:
    """Копия модели одного направления канала из симулятора с откалиброванными параметрами."""

    def __init__(self, inv_bw, half_rtt, c1, c2):
        self.inv_bw = inv_bw
        self.h = half_rtt
        self.c1 = c1  # штраф за пакет среди последних трех
        self.c2 = c2  # штраф за пакет сверх трех
        self.busy = 0.0
        self.last = 0.0

    def transmit(self, now, size):
        packets = max(1, math.ceil((size + FRAME) / MSS))
        wire = size + FRAME + packets * PKT
        start = now if now > self.busy else self.busy
        tx = wire * self.inv_bw
        self.busy = start + tx
        penalty = self.c2 * max(0, packets - 3) + self.c1 * min(packets, 3)
        arrival = start + tx + self.h + penalty
        if arrival < self.last:
            arrival = self.last
        self.last = arrival
        return arrival


class Client:
    def __init__(self, env, config):
        self.env = env
        self.ro = bool(config.get("read_only"))
        self.T = float(config.get("staleness_seconds", 1.0))
        self.next_req = 1
        self.waiting = {}  # req -> (op_id, op, норм. путь, предсказанный приход или None)
        self.sends = []  # (время, размер) всех отправок до калибровки
        self.unknown_arrivals = 0  # отправки, время прихода которых еще не известно
        self.probe_i = 0
        self.probe_out = None  # (req, время отправки, размер)
        self.probe_d = []
        self.probe_pending = False
        self.model = None
        self.wb = False  # write-back разрешен
        self.ns_wanted = False
        self.ns_inflight = False
        self.ns_replay = []
        self.kn = {}  # путь -> 'f', 'd', 'a' (нет), 'l' (EACCES)
        self.kids = {}  # каталог с полностью известным списком -> {имя: тип или '?'}
        self.last_async = 0.0  # предсказанный приход последней асинхронной мутации
        self.timers = {}
        self.level = 1  # уровень zlib для данных записи
        self.scache = {}  # путь -> (ts, тип или None для ENOENT, размер)
        self.dcache = {}  # путь -> [(ts, начало, данные, конец файла ли)]
        self.wtime = {}  # путь -> время прихода последней своей мутации на сервер
        self.ctr = 2  # счетчик: читает ли агент файл после stat
        self.watch = None
        self.pf = None  # чтение с опережением
        self.parked = None  # чтение агента, ждущее куска в полете
        self.last_read = None
        self.ratio = {}  # путь -> доля размера ответа от размера данных
        self.pf_ban = set()  # пути, где опережение уже дало устаревший кусок

    # отправка с учетом модели канала
    def send(self, data, track=True):
        now = self.env.now()
        pred = None
        if self.model is not None:
            pred = self.model.transmit(now, len(data))
        else:
            self.sends.append((now, len(data)))
            if track:
                self.unknown_arrivals += 1
        self.env.send(data)
        return pred

    # калибровка
    def on_start(self):
        self.send_probe()

    def send_probe(self):
        size = PROBE_SIZES[self.probe_i]
        req = self.next_req
        self.next_req += 1
        msg = HDR.pack(M_PROBE, 0, req)
        msg += b"\x00" * (size - len(msg))
        self.probe_out = (req, self.env.now(), len(msg), self.unknown_arrivals == 0)
        self.send(msg)

    def on_probe(self, req, t_arr):
        preq, t_send, size, clean = self.probe_out
        self.probe_out = None
        self.unknown_arrivals -= 1
        if clean:
            self.probe_d.append((size, t_arr - t_send))
            self.probe_i += 1
        if self.probe_i >= len(PROBE_SIZES):
            self.finish_calibration()
            return
        if self.unknown_arrivals == 0:
            self.send_probe()
        else:
            self.probe_pending = True

    def finish_calibration(self):
        (s1, d1), (s2, d2), (s3, d3), (s4, d4) = self.probe_d
        p4 = math.ceil((s4 + FRAME) / MSS)
        inv_bw = (d2 - d1) / (s2 - s1)
        c1 = (d3 - d1 - (s3 + 2 * PKT - s1) * inv_bw) / 2
        c2 = (d4 - d3 - (s4 - s3 + (p4 - 3) * PKT) * inv_bw) / (p4 - 3)
        h = d1 - (s1 + FRAME + PKT) * inv_bw - c1
        if not (inv_bw > 0 and h >= 0 and c1 >= -1e-9 and c2 >= -1e-9):
            return  # модель не сошлась, остаемся в синхронном режиме
        c1 = max(c1, 0.0)
        c2 = max(c2, 0.0)
        model = LinkModel(inv_bw, h, c1, c2)
        for t, size in self.sends:
            model.transmit(t, size)
        self.sends = []
        self.model = model
        self.wb = True
        self.level = 6 if inv_bw > 1.0 / 2e6 else 1
        self.send(HDR.pack(M_PARAMS, 0, 0) + struct.pack(">dddd", inv_bw, h, c1, c2))
        if not self.ro and h >= 0.005:
            self.ns_inflight = True
            self.ns_replay = []
            self.send(HDR.pack(M_NSREQ, 0, 0))

    # знание о дереве
    def lookup(self, p):
        v = self.kn.get(p)
        if v is not None:
            return v
        if p == "":
            return "d"
        kids = self.kids.get(parent_of(p))
        if kids is not None:
            t = kids.get(name_of(p))
            if t is None:
                return "a"
            if t != "?":
                return t
        return None

    def under_link(self, p):
        if not p:
            return False
        parts = p.split("/")
        prefix = ""
        for part in parts:
            prefix = prefix + "/" + part if prefix else part
            if self.lookup(prefix) == "l":
                return True
        return False

    def learn(self, p, t):
        """Факт о пути p. t это 'f', 'd', 'a', 'l' или None (забыть)."""
        if t is None:
            self.kn.pop(p, None)
        else:
            self.kn[p] = t
        if p == "":
            return
        par = parent_of(p)
        kids = self.kids.get(par)
        if kids is not None:
            name = name_of(p)
            if t == "a":
                kids.pop(name, None)
            else:
                kids[name] = t if t is not None else "?"
        if t == "a":
            self.kids.pop(p, None)
        if t in ("f", "d"):
            while par and self.kn.get(par) != "d":
                self.kn[par] = "d"
                par = parent_of(par)

    def forget(self, p, t_arr):
        self.scache.pop(p, None)
        self.dcache.pop(p, None)
        if t_arr is not None and t_arr > self.wtime.get(p, -1.0):
            self.wtime[p] = t_arr
        pf = self.pf
        if pf is not None and pf["p"] == p:
            self.pf = None

    def apply_mutation(self, op, p, q, t_arr=None):
        kind = op["op"]
        for path in (p, parent_of(p)) if not q else (p, parent_of(p), q, parent_of(q)):
            self.forget(path, t_arr)
        if kind in ("create", "write", "truncate"):
            self.learn(p, "f")
        elif kind == "mkdir":
            self.learn(p, "d")
            self.kids[p] = {}
        elif kind == "unlink":
            self.learn(p, "a")
        elif kind == "rename":
            self.learn(p, "a")
            self.learn(q, "f")

    def predict_ok(self, op, p, q):
        kind = op["op"]
        lk = self.lookup
        if kind == "create":
            return p != "" and lk(parent_of(p)) == "d" and lk(p) in ("f", "a")
        if kind == "write":
            return lk(p) == "f" and int(op["offset"]) >= 0
        if kind == "truncate":
            return lk(p) == "f" and int(op["size"]) >= 0
        if kind == "mkdir":
            return p != "" and lk(parent_of(p)) == "d" and lk(p) == "a"
        if kind == "unlink":
            return p != "" and lk(p) == "f"
        if kind == "rename":
            return p != "" and q != "" and p != q and lk(p) == "f" and lk(parent_of(q)) == "d" and lk(q) in ("f", "a")
        return False

    # кодирование запроса
    def encode(self, op, req, flags):
        kind = op["op"]
        body = enc_path(op.get("path", ""))
        payload = b""
        if kind == "read":
            body += struct.pack(">qq", int(op["offset"]), int(op["size"]))
        elif kind == "write":
            body += struct.pack(">q", int(op["offset"]))
            payload = bytes(op["data"])
            if len(payload) >= ZIP_MIN:
                z = zlib.compress(payload, self.level)
                if len(z) < len(payload) * 0.9:
                    payload = z
                    flags |= F_ZIP
        elif kind == "truncate":
            body += struct.pack(">q", int(op["size"]))
        elif kind == "rename":
            body += enc_path(op["dst"])
        return HDR.pack(OP_CODE[kind], flags, req) + body + payload

    # операции агента
    def on_op(self, op_id, op):
        env = self.env
        kind = op["op"]
        if kind == "flush":
            return self.do_flush(op_id, op)
        p = norm(op.get("path"))
        q = norm(op["dst"]) if kind == "rename" else ""
        if p is None or q is None:
            env.reply(op_id, {"ok": False, "error": "EACCES"})
            return
        if kind in MUTATIONS:
            if self.ro:
                env.reply(op_id, {"ok": False, "error": "EROFS"})
                return
            if self.wb and self.predict_ok(op, p, q):
                return self.write_back(op_id, op, p, q)
        elif self.under_link(p):
            env.reply(op_id, {"ok": False, "error": "EACCES"})
            return
        else:
            w = self.watch
            if w is not None and not (kind == "stat" and p == w):
                self.watch = None
                if kind == "read" and p == w:
                    if self.ctr < 3:
                        self.ctr += 1
                elif self.ctr > 0:
                    self.ctr -= 1
            if kind == "stat":
                if self.stat_cached(op_id, p):
                    return
                if self.ctr >= 2 and self.model is not None:
                    req = self.next_req
                    self.next_req += 1
                    pred = self.send(self.encode(op, req, F_WANT))
                    self.waiting[req] = (op_id, op, p, q, pred)
                    return
            elif kind == "read":
                if self.do_read(op_id, op, p):
                    return
        req = self.next_req
        self.next_req += 1
        pred = self.send(self.encode(op, req, 0))
        self.waiting[req] = (op_id, op, p, q, pred)

    # кэш со сроком T от времени чтения на сервере
    def stat_cached(self, op_id, p):
        e = self.scache.get(p)
        if e is None or self.env.now() >= e[0] + self.T - FRESH:
            return False
        if e[1] is None:
            self.env.reply(op_id, {"ok": False, "error": "ENOENT"})
        else:
            self.env.reply(op_id, {"ok": True, "type": e[1], "size": e[2]})
            if e[1] == "file":
                self.watch = p
        return True

    def find_seg(self, p, off, size, t0):
        segs = self.dcache.get(p)
        if not segs:
            return None
        limit = t0 - self.T + FRESH
        for ts, start, data, eof in segs:
            if ts > limit and start <= off and (off + size <= start + len(data) or eof):
                return data[off - start : off - start + size]
        return None

    def add_seg(self, p, ts, start, data, eof):
        if ts <= self.wtime.get(p, -1.0):
            return  # прочитано до прихода своей мутации
        limit = self.env.now() - self.T
        segs = [s for s in self.dcache.get(p, ()) if s[0] > limit]
        segs.append((ts, start, data, eof))
        self.dcache[p] = segs

    def do_read(self, op_id, op, p):
        """True, если чтение обслужено или отложено без нового запроса."""
        env = self.env
        off, size = int(op["offset"]), int(op["size"])
        if off < 0 or size < 0:
            return False
        now = env.now()
        data = self.find_seg(p, off, size, now)
        pf = self.pf
        mine = pf is not None and pf["p"] == p
        if data is not None:
            seq = not mine and self.is_seq(p, off, size)
            self.last_read = (p, off + size)
            env.reply(op_id, {"ok": True, "data": data})
            if mine:
                self.pf_fill(off)
            elif seq and len(data) == size and self.model is not None and p not in self.pf_ban:
                self.start_pf(p, off, size)
            return True
        if mine and off in pf["out"] and pf["chunk"] >= size:
            self.parked = (op_id, op, p, off, size, now)
            self.last_read = (p, off + size)
            self.pf_fill(off)
            return True
        if mine:
            if any(s[1] == off for s in self.dcache.get(p, ())):
                self.kill_pf(p)  # кусок пришел, но устарел
            else:
                self.pf = None  # чтение не по порядку
        seq = self.is_seq(p, off, size)
        self.last_read = (p, off + size)
        req = self.next_req
        self.next_req += 1
        pred = self.send(self.encode(op, req, 0))
        self.waiting[req] = (op_id, op, p, "", pred)
        if seq and self.model is not None and p not in self.pf_ban:
            self.start_pf(p, off, size)
        return True

    def is_seq(self, p, off, size):
        """Похоже ли чтение на последовательное чтение большого файла."""
        if off > 0:
            return self.last_read == (p, off)
        if size < BIG_READ:
            return False
        e = self.scache.get(p)
        return e is not None and e[1] == "file" and e[2] > size

    def kill_pf(self, p):
        self.pf = None
        self.pf_ban.add(p)

    def pf_window(self, p, chunk):
        """Сколько кусков держать в полете впереди агента.

        Первый залп запросов сервер читает почти одновременно, k-й кусок ждет в очереди
        k передач. Поэтому k * tx + h + штраф должно уложиться в T с запасом. Для занятого
        канала нужно, чтобы k * tx покрывало круг запроса.
        """
        m = self.model
        w = int(chunk * self.ratio.get(p, 1.0)) + RHDR.size
        packets = max(1, math.ceil((w + FRAME) / MSS))
        tx = (w + FRAME + packets * PKT) * m.inv_bw
        pen = m.c1 * min(packets, 3) + m.c2 * max(0, packets - 3)
        k_pipe = math.ceil((2 * m.h + m.c1 + pen) / tx) + 1
        k_fresh = math.floor((self.T - FRESH - 0.05 - m.h - pen) / tx)
        return max(0, min(k_pipe, k_fresh, PF_MAX))

    def start_pf(self, p, pos, chunk):
        k = self.pf_window(p, chunk)
        if k < 1:
            self.pf = None
            return
        e = self.scache.get(p)
        end = e[2] if e is not None and e[1] == "file" else None
        self.pf = {"p": p, "next": pos + chunk, "chunk": chunk, "out": {}, "end": end, "K": k, "eof": False}
        self.pf_fill(pos)

    def pf_fill(self, pos):
        pf = self.pf
        if pf is None:
            return
        out = pf["out"]
        p = pf["p"]
        chunk = pf["chunk"]
        limit = pos + pf["K"] * chunk
        now = self.env.now()
        while not pf["eof"] and pf["next"] <= limit and (pf["end"] is None or pf["next"] <= pf["end"]):
            off = pf["next"]
            if off not in out and self.find_seg(p, off, chunk, now) is None:
                op = {"op": "read", "path": p, "offset": off, "size": chunk}
                req = self.next_req
                self.next_req += 1
                pred = self.send(self.encode(op, req, 0))
                self.waiting[req] = (-1, op, p, "", pred)
                out[off] = req
            pf["next"] = off + chunk

    def on_prefetch(self, op, p, status, t_arr, data):
        off, size = int(op["offset"]), int(op["size"])
        pf = self.pf
        mine = pf is not None and pf["p"] == p and off in pf["out"]
        if mine:
            del pf["out"][off]
        if not status:
            eof = len(data) < size
            self.add_seg(p, t_arr, off, data, eof)
            if mine and eof:
                pf["eof"] = True
            if mine:
                pf["K"] = self.pf_window(p, size)
        elif mine:
            self.pf = None
        park = self.parked
        if park is not None and park[2] == p and park[3] == off:
            self.parked = None
            op_id, pop, _, poff, psize, t0 = park
            got = self.find_seg(p, poff, psize, t0) if not status else None
            if got is not None:
                self.env.reply(op_id, {"ok": True, "data": got})
            else:
                self.kill_pf(p)
                req = self.next_req
                self.next_req += 1
                pred = self.send(self.encode(pop, req, 0))
                self.waiting[req] = (op_id, pop, p, "", pred)

    def write_back(self, op_id, op, p, q):
        env = self.env
        now = env.now()
        pred = self.send(self.encode(op, 0, F_ASYNC), track=False)
        self.apply_mutation(op, p, q, pred)
        if self.ns_inflight:
            self.ns_replay.append((op, p, q))
        if pred > self.last_async:
            self.last_async = pred
        ack_at = pred - self.T + MARGIN
        if ack_at <= now:
            env.reply(op_id, {"ok": True})
        else:
            key = ("ack", op_id)
            self.timers[key] = {"ok": True}
            env.set_timer(ack_at - now, key)

    def do_flush(self, op_id, op):
        env = self.env
        now = env.now()
        if self.ro and not self.waiting:
            env.reply(op_id, {"ok": True})  # в режиме только для чтения агент ничего не менял
            return
        if self.model is not None and self.wb and not self.waiting:
            done_at = self.last_async + MARGIN
            if done_at <= now:
                env.reply(op_id, {"ok": True})
            else:
                key = ("ack", op_id)
                self.timers[key] = {"ok": True}
                env.set_timer(done_at - now, key)
            return
        req = self.next_req
        self.next_req += 1
        pred = self.send(HDR.pack(OP_CODE["flush"], 0, req))
        self.waiting[req] = (op_id, op, "", "", pred)

    # сообщения сервера
    def on_message(self, data):
        kind, flags, status, req, t_arr = RHDR.unpack_from(data)
        body = data[RHDR.size :]
        if kind == R_RESULT:
            return self.on_result(flags, status, req, t_arr, body)
        if kind == R_PROBE:
            self.on_probe(req, t_arr)
            if self.probe_pending and self.unknown_arrivals == 0 and self.model is None:
                self.probe_pending = False
                self.send_probe()
            return
        if kind == R_NOTIFY:
            p = norm(body.decode())
            if p is not None:
                self.learn(p, None)
                self.scache.pop(p, None)
                self.dcache.pop(p, None)
            return
        if kind == R_NS:
            self.load_ns(zlib.decompress(body))
            return

    def load_ns(self, raw):
        tree = json.loads(raw)
        kids = {}
        kn = {}
        for d, (files, dirs, links, other) in tree.items():
            m = {}
            for n in other:
                m[n] = "?"
            for n in files:
                m[n] = "f"
            for n in dirs:
                m[n] = "d"
            for n in links:
                m[n] = "l"
                kn[d + "/" + n if d else n] = "l"
            kids[d] = m
        self.kids = kids
        self.kn = kn
        for op, p, q in self.ns_replay:
            self.apply_mutation(op, p, q)
        self.ns_replay = []
        self.ns_inflight = False

    def on_result(self, flags, status, req, t_arr, body):
        op_id, op, p, q, pred = self.waiting.pop(req)
        if op_id == -1:
            if not status:
                wire = len(body)
                if flags & F_ZIP:
                    body = zlib.decompress(body)
                if body:
                    self.ratio[p] = wire / len(body)
            return self.on_prefetch(op, p, status, t_arr, body)
        if self.model is None:
            self.unknown_arrivals -= 1
            if self.probe_pending and self.unknown_arrivals == 0:
                self.probe_pending = False
                self.send_probe()
        elif pred is not None and t_arr > pred + 1e-7:
            # модель канала ошиблась: дальше только синхронный режим
            self.wb = False
        kind = op["op"]
        if status:
            err = ERRS[status]
            res = {"ok": False, "error": err}
            if kind == "stat" and err == "ENOENT" and t_arr > self.wtime.get(p, -1.0):
                self.scache[p] = (t_arr, None, 0)
            if kind == "stat" or kind == "read" or kind == "listdir":
                if err == "ENOENT":
                    if self.lookup(parent_of(p)) == "d":
                        self.learn(p, "a")
                elif err == "EACCES":
                    self.kn[p] = "l"
                elif err == "EISDIR":
                    self.learn(p, "d")
                elif err == "ENOTDIR" and kind == "listdir":
                    self.learn(p, "f")
            self.env.reply(op_id, res)
            return
        if kind == "stat":
            t, size = struct.unpack_from(">BQ", body)
            typ = TYPES.get(t, "file")
            self.learn(p, "f" if typ == "file" else "d")
            res = {"ok": True, "type": typ, "size": size}
            if t_arr > self.wtime.get(p, -1.0):
                self.scache[p] = (t_arr, typ, size)
                if flags & F_DATA:
                    data = body[9:]
                    if flags & F_ZIP:
                        data = zlib.decompress(data)
                    self.add_seg(p, t_arr, 0, data, bool(flags & F_EOF))
            if typ == "file":
                self.watch = p
        elif kind == "listdir":
            if flags & F_ZIP:
                body = zlib.decompress(body)
            entries = body.decode().split("\x00") if body else []
            self.learn(p, "d")
            old = self.kids.get(p) or {}
            m = {}
            for n in entries:
                t = self.kn.get(p + "/" + n if p else n) or old.get(n) or "?"
                m[n] = t
            self.kids[p] = m
            res = {"ok": True, "entries": entries}
        elif kind == "read":
            wire = len(body)
            if flags & F_ZIP:
                body = zlib.decompress(body)
            if body:
                self.ratio[p] = wire / len(body)
            self.learn(p, "f")
            res = {"ok": True, "data": body}
            size = int(op["size"])
            self.add_seg(p, t_arr, int(op["offset"]), body, len(body) < size)
        else:
            if kind in MUTATIONS:
                self.apply_mutation(op, p, q, t_arr)
            res = {"ok": True}
        self.env.reply(op_id, res)

    def on_timer(self, key):
        res = self.timers.pop(key, None)
        if res is not None:
            self.env.reply(key[1], res)
