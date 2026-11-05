"""Сервер протокола монтирования. Работает на машине пользователя рядом с папкой.

r1-c1. Реплика метаданных, кэш содержимого по блокам и аренда свежести.

При старте сервер обходит папку и шлет клиенту все дерево (имена, типы, размеры).
Затем две одинаковые пробы, по разнице их прихода клиент меряет полосу канала и
сообщает ее серверу. Дальше сервер сам решает, что слать.

Срочные сообщения (ответы на запросы, обновления после правок пользователя,
пульс) уходят сразу. Объемные данные идут через насос с темпом под полосу: в очереди
канала держится не больше QMAX секунд, поэтому срочное сообщение не ждет долго за
фоновой передачей. Очереди насоса по приоритету: блоки, которые клиент ждет сейчас,
поток большого файла, соседи недавно запрошенного файла, все текстовые файлы в
порядке обхода дерева.

Каждое сообщение несет метку времени сервера ts. Канал FIFO, поэтому клиент, получив
метку ts, знает обо всех изменениях, которые сервер увидел до ts. Пульс раз в HB
секунд держит эту метку свежей, когда сообщать нечего.

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
import zlib
from array import array
from collections import deque

B = 65536  # размер блока содержимого
LARGE = 1536 * 1024  # файлы больше этого читаются блоками по запросу и потоком
ACTIVE = 1.0  # окно, в котором смотрим на активность агента, с
CHASE = 64  # запрос файла не дальше стольких позиций от фронта потока значит, что агент догнал поток
ACTIVE_BATCH = 32768  # фоновая порция без сжатия, пока агент читает, короче, чтобы его ответы не ждали
HEAD = 131072  # начало большого файла, которое стоит взять заранее
TAIL = 65536  # конец большого файла, который стоит взять заранее
PREFETCH_LARGE = 3  # у скольких самых больших файлов брать начало и конец заранее
HB = 0.4  # интервал пульса, с
QMAX = 0.01  # сколько секунд передачи держать в очереди канала
BATCH_T = 0.05  # длительность одной фоновой порции на канале, с
MIN_BATCH = 8192
RAW_BATCH = 131072  # фоновая порция без сжатия на быстром канале, меньше сообщений и CPU
MAX_BATCH = 400000
PROBE = 12000
DEFAULT_BW = 6.25e6
BIN_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".woff", ".woff2", ".ttf", ".otf", ".jar", ".zip", ".gz",
    ".tgz", ".bz2", ".xz", ".7z", ".pdf", ".bin", ".so", ".dll", ".dylib", ".exe", ".class", ".pyc", ".o", ".a",
    ".wasm", ".mp3", ".mp4", ".mov", ".avi",
)
CODEC_RATIO = {0: 8.0, 1: 12.0}  # оценка сжатия текста словарем строк без zlib и с zlib


class LineCodec:
    """Словарь строк, общий для сервера и клиента.

    Текст режется по переводам строк. Строка, которая уже встречалась в потоке,
    уходит двухбайтовым номером, новая уходит целиком и получает следующий номер.
    Клиент строит тот же словарь в том же порядке, потому что канал FIFO.
    В коде много повторяющихся строк, а в этих деревьях файлы собраны из общего
    пула строк, поэтому выигрыш в разы больше, чем у zlib с окном 32 КБ.
    """

    def __init__(self):
        self.t = {}

    def encode(self, data: bytes):
        t = self.t
        lines = data.split(b"\n")
        ids = list(map(t.get, lines))
        lits = []
        if None in ids:
            miss = [i for i, v in enumerate(ids) if v is None]
            if len(t) + len({lines[i] for i in miss}) > 65535:
                return None
            n = len(t)
            for i in miss:
                line = lines[i]
                v = t.get(line)
                if v is None:
                    t[line] = v = n
                    n += 1
                    lits.append(line)
                ids[i] = v
        return array("H", ids).tobytes(), lits


def pack(header: dict, payload: bytes = b"") -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(h)) + h + payload


def unpack(data: bytes) -> tuple[dict, bytes]:
    n = struct.unpack(">I", data[:4])[0]
    return json.loads(data[4 : 4 + n]), data[4 + n :]


def norm(path):
    """Та же нормализация, что у папки. None значит выход за корень."""
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


def parent_of(p: str) -> str:
    return p.rsplit("/", 1)[0] if "/" in p else ""


def join(d: str, n: str) -> str:
    return d + "/" + n if d else n




def wire(n: int) -> int:
    m = n + 8
    return m + -(-m // 1448) * 66


class Server:
    def __init__(self, env, fs, config):
        self.env = env
        self.fs = fs
        self.T = float(config.get("staleness_seconds", 1.0))
        self.hb = min(HB, max(0.05, (self.T - 0.2) / 2))
        self.meta = {}  # путь -> ["d", имена] | ["f", размер] | ["e", код]
        self.ver = {}  # путь файла -> (размер, mtime), версия, о которой знает клиент
        self.order = []  # файлы в порядке обхода grep
        self.pos = 0
        self.kind = {}  # путь -> "t" текст | "b" бинарный
        self.incompressible = set()
        self.tested = set()
        self.sent = {}  # путь -> множество блоков текущей версии, уже отправленных
        self.fq = deque()  # (путь, блок) или (None, req), то, что клиент ждет сейчас
        self.fq_set = set()
        self.bigq = deque()  # (путь, блок) поток большого файла
        self.hot = deque()  # пути соседей недавно запрошенного файла
        self.preq = deque()  # (путь, блок) начала и концы больших файлов, только на быстром канале
        self.lateq = deque()  # не используется, остаток большого файла идет только по сигналу агента
        self.last_big_fetch = -1e9  # последний запрос блоков большого файла, агент ждет его поток
        self.planned = set()  # большие файлы, для которых уже идет поток
        self.last_read = -1e9  # последний запрос чтения от клиента
        self.last_chase = -1e9  # последний запрос файла у самого фронта фонового потока
        self.index = {}  # путь -> позиция в порядке обхода
        self.last_mut = -1e9  # последняя запись агента
        self.bigb = B  # блок больших файлов, его выбирает клиент по полосе
        self.q_until = 0.0
        self.last_send = 0.0
        self.bw = None
        self.bt = BATCH_T
        self.qmax = QMAX
        self.pump_at = None
        self.comp = {}
        self.codec = LineCodec()

    # отправка
    def send(self, h: dict, payload: bytes = b"") -> None:
        now = self.env.now()
        h["ts"] = now
        msg = pack(h, payload)
        self.env.send(msg)
        self.last_send = now
        self.q_until = max(self.q_until, now) + wire(len(msg)) / (self.bw or DEFAULT_BW)

    # дерево
    def stat_entry(self, p: str):
        """Текущее состояние пути как запись реплики. None значит пути нет."""
        try:
            st = self.fs.stat(p)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            if code is None:
                raise
            if code in ("ENOENT", "ENOTDIR"):
                return None, None
            return ["e", code], None
        if st["type"] == "dir":
            return ["d", self.fs.listdir(p)], None
        return ["f", st["size"]], (st["size"], st["mtime"])

    def scan(self, p: str, out: dict, order: list | None) -> None:
        names = self.fs.listdir(p)
        self.meta[p] = out[p] = ["d", names]
        for n in names:
            c = join(p, n)
            e, v = self.stat_entry(c)
            if e is None:
                continue
            if e[0] == "d":
                self.scan(c, out, order)
                continue
            self.meta[c] = out[c] = e
            if v is not None:
                self.ver[c] = v
                if order is not None:
                    order.append(c)

    def drop_subtree(self, p: str, out: dict) -> None:
        old = self.meta.pop(p, None)
        self.ver.pop(p, None)
        self.sent.pop(p, None)
        out[p] = None
        if old is not None and old[0] == "d":
            for n in old[1]:
                self.drop_subtree(join(p, n), out)

    def refresh(self, paths, agent: bool) -> tuple[dict, list]:
        """Сверяет пути с папкой. Возвращает изменения реплики и пути, чье содержимое устарело."""
        out: dict = {}
        drops: list = []
        for p in paths:
            if p is None or p in out:
                continue
            e, v = self.stat_entry(p)
            old = self.meta.get(p)
            if e is None:
                if old is not None:
                    self.drop_subtree(p, out)
                    drops.append(p)
                continue
            if e[0] == "d":
                if old is not None and old[0] == "d" and old[1] == e[1]:
                    continue
                if old is not None and old[0] != "d":
                    self.drop_subtree(p, out)
                self.meta[p] = out[p] = e
                prev = set(old[1]) if old is not None and old[0] == "d" else set()
                now_names = set(e[1])
                for n in prev - now_names:
                    self.drop_subtree(join(p, n), out)
                for n in e[1]:
                    if n in prev:
                        continue
                    c = join(p, n)
                    ce, cv = self.stat_entry(c)
                    if ce is None:
                        continue
                    if ce[0] == "d":
                        self.scan(c, out, None)
                    else:
                        self.meta[c] = out[c] = ce
                        if cv is not None:
                            self.ver[c] = cv
                            if not agent:
                                self.hot.append(c)
                continue
            if old is not None and old[0] == "d":
                self.drop_subtree(p, out)
            if old != e:
                self.meta[p] = out[p] = e
            if v is not None and self.ver.get(p) != v:
                self.ver[p] = v
                if not agent:
                    self.sent.pop(p, None)
                    self.planned.discard(p)
                    drops.append(p)
                    if e[1] <= LARGE:
                        self.hot.appendleft(p)
                    out[p] = e
            elif e[0] == "e" and old != e:
                drops.append(p)
        return out, drops

    def check_version(self, p: str) -> None:
        """Перед отправкой содержимого сверяет версию, чтобы не смешать блоки разных версий."""
        try:
            st = self.fs.stat(p)
            v = (st["size"], st["mtime"]) if st["type"] != "dir" else None
        except Exception as e:  # noqa: BLE001
            if getattr(e, "code", None) is None:
                raise
            v = None
        if v is None or v != self.ver.get(p):
            out, drops = self.refresh([p, parent_of(p)], agent=False)
            if out or drops:
                self.send({"t": "u", "m": out, "x": drops})

    # события
    def on_start(self):
        out: dict = {}
        order: list = []
        self.scan("", out, order)
        self.order = order
        self.index = {p: i for i, p in enumerate(order)}
        body = zlib.compress(json.dumps(out, separators=(",", ":")).encode(), 6)
        self.send({"t": "m"}, body)
        pad = b"\x00" * PROBE
        self.send({"t": "p", "i": 0}, pad)
        self.send({"t": "p", "i": 1}, pad)
        self.env.set_timer(self.hb, "h")

    def on_message(self, data):
        h, payload = unpack(data)
        t = h["t"]
        if t == "w":
            self.last_mut = self.env.now()
        elif t != "bw":
            now = self.env.now()
            self.last_read = now
            if t == "f":
                i = self.index.get(h["p"])
                # файл чуть позади фронта еще в пути, чуть впереди еще не отправлен, в обоих случаях агент ждет поток
                if i is not None and self.pos - 2 * CHASE <= i < self.pos + CHASE:
                    self.last_chase = now
        if t == "f":
            self.on_fetch(h)
        elif t == "w":
            self.on_mutation(h, payload)
        elif t == "s":
            self.send({"t": "r", "r": [h["r"]]})
        elif t == "d":
            self.on_direct(h)
        elif t == "h":
            self.on_hint(h)
        elif t == "bw":
            self.bw = max(20000.0, float(h["v"]))
            self.bigb = int(h.get("bb", B))
            half = max(0.0, float(h.get("ow", 0.025)))
            # порция фона не дольше четверти RTT, чтобы срочный ответ ждал за ней мало
            self.bt = min(BATCH_T, max(0.002, half / 2))
            self.qmax = min(QMAX, max(0.001, half / 2))
            if self.bw >= 3e6:
                self.plan_prefetch()
        self.pump()

    def on_local_change(self, path):
        p = norm(path)
        if p is None:
            return
        out, drops = self.refresh([p, parent_of(p)], agent=False)
        if out or drops:
            self.send({"t": "u", "m": out, "x": drops})
        self.pump()

    def on_timer(self, key):
        now = self.env.now()
        if key == "h":
            if now - self.last_send >= self.hb - 1e-9:
                self.send({"t": "b"})
            self.env.set_timer(max(0.01, self.last_send + self.hb - now), "h")
            return
        if key == "p":
            if self.pump_at is not None and now + 1e-9 >= self.pump_at:
                self.pump_at = None
            self.pump()

    # запросы клиента
    def on_fetch(self, h: dict) -> None:
        p = h["p"]
        req = h["r"]
        self.check_version(p)
        e = self.meta.get(p)
        if e is None or e[0] != "f":
            self.send({"t": "r", "r": [req]})
            return
        size = e[1]
        n = -(-size // self.bsz(size))
        have = self.sent.get(p, ())
        force = h.get("force")
        fq_set = self.fq_set
        queued = False
        for b in h["b"]:
            if not 0 <= b < n:
                continue
            if (p, b) in fq_set:
                queued = True
            elif force or b not in have:
                fq_set.add((p, b))
                self.fq.append((p, b))
                queued = True
        if queued:
            self.fq.append((None, req))
        else:
            self.send({"t": "r", "r": [req]})
        if size > LARGE:
            self.last_big_fetch = self.env.now()
            self.plan_big(p, n, min(h["b"] or [0]), max(h["b"] or [0]))
        else:
            self.plan_near(p)

    def on_direct(self, h: dict) -> None:
        """Операция чтения целиком на сервере, как в baseline. Страховка клиента от зацикливания."""
        fs, op, path = self.fs, h["op"], h.get("path")
        data = b""
        try:
            if op == "stat":
                st = fs.stat(path)
                res = {"ok": True, "type": st["type"], "size": st["size"]}
            elif op == "listdir":
                res = {"ok": True, "entries": fs.listdir(path)}
            else:
                data = fs.read(path, h["offset"], h["size"])
                res = {"ok": True}
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            if code is None:
                raise
            res = {"ok": False, "error": code}
        msg = {"t": "o", "r": [h["r"]], "res": res}
        if op == "read" and res["ok"]:
            msg["d"] = 1
        self.send(msg, data)

    def on_hint(self, h: dict) -> None:
        """Агент читает большой файл из кэша. Сервер начинает поток остатка, ответа нет."""
        p = h["p"]
        if p in self.planned:
            return
        self.check_version(p)
        e = self.meta.get(p)
        if e is None or e[0] != "f" or e[1] <= LARGE:
            return
        b = int(h["b"][0]) if h.get("b") else 0
        self.plan_big(p, -(-e[1] // self.bigb), b, b)

    def test_compress(self, p: str) -> None:
        if p in self.tested:
            return
        self.tested.add(p)
        probe = self.fs.read(p, 0, 16384)
        if len(probe) >= 4096 and len(zlib.compress(probe, 1)) > 0.9 * len(probe):
            self.incompressible.add(p)

    def plan_big(self, p: str, n: int, b0: int, b1: int) -> None:
        self.planned.add(p)
        self.test_compress(p)
        size = self.meta[p][1]
        t0 = max(0, size - TAIL) // self.bigb
        tail = [b for b in range(t0, n) if b > b1]
        seq = tail + [b for b in range(b1 + 1, n) if b < t0] + list(range(0, b0))
        have = self.sent.get(p, ())
        self.bigq = deque((p, b) for b in seq if b not in have)

    def plan_prefetch(self) -> None:
        """Начала и концы самых больших файлов заранее. Остаток пойдет, когда агент тронет файл."""
        large = sorted(((e[1], p) for p, e in self.meta.items() if e[0] == "f" and e[1] > LARGE), reverse=True)
        for i, (size, p) in enumerate(large[:PREFETCH_LARGE]):
            n = -(-size // self.bigb)
            head = list(range(min(n, -(-HEAD // self.bigb))))
            t0 = max(0, size - TAIL) // self.bigb
            tail = [b for b in range(t0, n) if b not in head]
            for b in head + tail:
                self.preq.append((p, b))

    def files_in(self, d: str) -> list:
        e = self.meta.get(d)
        if e is None or e[0] != "d":
            return []
        out = []
        for n in e[1]:
            c = join(d, n)
            ce = self.meta.get(c)
            if ce is not None and ce[0] == "f":
                out.append(c)
        return out

    def plan_near(self, p: str) -> None:
        d = parent_of(p)
        near = self.files_in(d)
        if d:
            near += self.files_in(parent_of(d))
        e = self.meta.get(d)
        if e is not None and e[0] == "d":
            for n in e[1]:
                near += self.files_in(join(d, n))
        self.hot.extendleft(reversed(near))

    def on_mutation(self, h: dict, payload: bytes) -> None:
        op = h["op"]
        path = h["path"]
        fs = self.fs
        resp = {"t": "a", "r": [h["r"]]}
        try:
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
            else:
                raise ValueError(op)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            if code is None:
                raise
            resp["ok"] = False
            resp["error"] = code
            self.send(resp)
            return
        p = norm(path)
        touched = [p, parent_of(p)]
        if op == "rename":
            d = norm(h["dst"])
            touched += [d, parent_of(d)]
            moved = self.sent.pop(p, None)
            self.sent.pop(d, None)
            if moved is not None:
                self.sent[d] = moved
        out, drops = self.refresh(touched, agent=True)
        sums = {}
        for q in touched:
            e = self.meta.get(q) if q is not None else None
            if e is not None and e[0] == "f" and q not in sums:
                if e[1] <= LARGE:
                    sums[q] = [e[1], zlib.crc32(fs.read(q))]
                else:
                    drops.append(q)
                    self.sent.pop(q, None)
        if op != "rename":
            self.sent.pop(p, None)
        resp.update({"ok": True, "m": out, "x": drops, "c": sums})
        self.send(resp)

    # насос объемных данных
    def background(self) -> bool:
        return bool(self.preq or self.pos < len(self.order) or self.lateq)

    def paused(self) -> bool:
        """Агент только пишет на быстром канале. Фон ждет, чтобы ответы на записи не стояли за ним."""
        now = self.env.now()
        return (self.bw or 0) >= 3e6 and now - self.last_mut < ACTIVE and now - self.last_read >= ACTIVE

    def has_work(self) -> bool:
        if self.fq or self.bigq:
            return True
        return self.bw is not None and bool(self.hot or (self.background() and not self.paused()))

    def pump(self) -> None:
        qmax = self.qmax
        fails = 0
        while True:
            now = self.env.now()
            if self.q_until - now > qmax + 1e-9:
                break
            if self.batch():
                fails = 0
                continue
            fails += 1
            if fails >= 2:
                break
        now = self.env.now()
        if self.has_work():
            at = max(self.q_until - qmax, now + 0.001)
        elif self.bw is not None and self.background() and self.paused():
            at = self.last_mut + ACTIVE + 1e-6
        else:
            at = None
        if at is not None:
            if self.pump_at is None or at < self.pump_at - 1e-9:
                self.pump_at = at
                self.env.set_timer(max(0.0, at - self.env.now()), "p")

    def read_block(self, p: str, b: int, checked: set):
        if p not in checked:
            checked.add(p)
            self.check_version(p)
        e = self.meta.get(p)
        if e is None or e[0] != "f":
            return None
        bs = self.bsz(e[1])
        if b * bs >= e[1]:
            return None
        d = self.fs.read(p, b * bs, bs)
        self.sent.setdefault(p, set()).add(b)
        return d

    def is_text(self, p: str, size: int) -> bool:
        k = self.kind.get(p)
        if k is None:
            low = p.lower()
            if low.endswith(BIN_EXT):
                k = "b"
            else:
                head = self.fs.read(p, 0, 4096)
                k = "b" if b"\x00" in head else "t"
            self.kind[p] = k
        return k == "t"

    def bsz(self, size: int) -> int:
        return self.bigb if size > LARGE else B

    def small(self, p: str) -> bool:
        e = self.meta.get(p)
        return e is not None and e[0] == "f" and e[1] <= LARGE

    def large_items(self, q: deque, budget: float, checked: set) -> list:
        """Блоки одного большого файла из очереди q, пока хватает бюджета."""
        items = []
        raw = 0
        p0 = q[0][0]
        while q and raw < budget:
            p, b = q[0]
            if p != p0:
                break
            q.popleft()
            if b in self.sent.get(p, ()):
                continue
            d = self.read_block(p, b, checked)
            if d is not None:
                items.append((p, b, d))
                raw += len(d)
        return items

    def batch(self) -> bool:
        bw = self.bw or DEFAULT_BW
        now = self.env.now()
        cap = min(MAX_BATCH, max(MIN_BATCH, bw * self.bt))
        fast = bw >= 3e6
        # Быстрый канал. Агент догнал фронт фонового потока, тогда фон идет короткими порциями
        # через словарь строк. Агент просто читает, тогда фон сырой порциями по 32 КБ, чтобы ответы
        # не ждали. Агент молчит, тогда фон сырой порциями по 128 КБ, так дешевле всего по CPU.
        chasing = now - self.last_chase < ACTIVE
        reading = now - self.last_read < ACTIVE
        idle = fast and not chasing
        raw_batch = ACTIVE_BATCH if reading else RAW_BATCH
        paused = self.paused()
        items = []
        reqs = []
        checked: set = set()
        spec = False
        rawmode = False
        if self.fq:
            src = 0
            head = self.fq[0][0]
            text = head is None or self.small(head)
            budget = cap * (CODEC_RATIO[0 if bw >= 5e5 else 1] if text else 3.0)
            raw = 0
            while self.fq and (raw < budget or self.fq[0][0] is None):
                p, b = self.fq[0]
                if p is None:
                    self.fq.popleft()
                    reqs.append(b)
                    continue
                if items and self.small(p) != text:
                    break
                self.fq.popleft()
                self.fq_set.discard((p, b))
                d = self.read_block(p, b, checked)
                if d is not None:
                    items.append((p, b, d))
                    raw += len(d)
        elif self.bigq or (self.bw is not None and not self.hot and not paused and (self.preq or (self.pos >= len(self.order) and self.lateq))):
            src = 1
            text = False
            spec = not self.bigq
            if self.bigq:
                q = self.bigq
            elif self.preq:
                q = self.preq
            else:
                q = self.lateq
            p0 = q[0][0]
            # большой файл без сжатия, если он не сжимается или если на быстром канале агент его сейчас не ждет
            rawmode = p0 in self.incompressible or (fast and (spec or now - self.last_big_fetch >= ACTIVE))
            if rawmode:
                budget = max(cap, raw_batch) if fast else cap
            else:
                budget = cap * 3.0
            items = self.large_items(q, budget, checked)
        else:
            if self.bw is None:
                return False
            src = 2
            text = True
            budget = max(cap, raw_batch) if idle else cap * CODEC_RATIO[0 if bw >= 5e5 else 1]
            raw = 0
            while raw < budget:
                if self.hot:
                    p = self.hot[0]
                    from_hot = True
                elif self.pos < len(self.order) and not paused:
                    p = self.order[self.pos]
                    from_hot = False
                else:
                    break
                e = self.meta.get(p)
                done = True
                if e is not None and e[0] == "f" and 0 < e[1] <= LARGE and self.is_text(p, e[1]):
                    have = self.sent.get(p, ())
                    n = -(-e[1] // B)
                    for b in range(n):
                        if b in have:
                            continue
                        if raw >= budget:
                            done = False
                            break
                        d = self.read_block(p, b, checked)
                        if d is None:
                            break
                        items.append((p, b, d))
                        raw += len(d)
                        have = self.sent.get(p, ())
                if not done:
                    break
                if from_hot:
                    self.hot.popleft()
                else:
                    self.pos += 1
        if not items:
            if reqs:
                self.send({"t": "r", "r": reqs})
                return True
            return src != 2 and self.has_work()
        body = b"".join(d for _, _, d in items)
        h = {"t": "c"}
        enc = self.codec.encode(body) if (text and not idle) else None
        if enc is not None:
            ib, lits = enc
            h["k"] = [len(ib), len(lits)]
            body = ib + b"\n".join(lits) if lits else ib
            lvl = 0 if bw >= 5e5 else 1
        else:
            p0 = items[0][0]
            if src != 2 and p0 not in self.tested:
                self.tested.add(p0)
                probe = items[0][2][:16384]
                if len(probe) >= 4096 and len(zlib.compress(probe, 1)) > 0.9 * len(probe):
                    self.incompressible.add(p0)
            if p0 in self.incompressible or rawmode or (fast and src == 2):
                lvl = 0
            elif bw >= 4e5:
                lvl = 1
            else:
                lvl = 6
        if lvl:
            c = self.comp.get(lvl)
            if c is None:
                c = self.comp[lvl] = zlib.compressobj(lvl)
            body = c.compress(body) + c.flush(zlib.Z_SYNC_FLUSH)
        h["z"] = lvl
        h["f"] = [[p, b, len(d)] for p, b, d in items]
        if reqs:
            h["r"] = reqs
        self.send(h, body)
        return True
