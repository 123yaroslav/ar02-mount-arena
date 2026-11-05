"""Сервер протокола монтирования. Работает на машине пользователя рядом с папкой.

r2-c1. Выполняет запросы клиента. Асинхронные мутации (write-back) применяет без ответа.
На пробы калибровки отвечает временем прихода. По запросу отдает снимок пространства
имен (каталоги, файлы, ссылки) сжатым одним сообщением. О правках пользователя сообщает
клиенту, чтобы тот забыл устаревшее знание о пути. Данные чтения сжимает zlib, если
содержимое сжимается.

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

OPS = ["stat", "listdir", "read", "create", "write", "truncate", "mkdir", "unlink", "rename", "flush"]
M_PROBE = 20
M_NSREQ = 21
M_PARAMS = 22
F_ASYNC = 1
F_ZIP = 2

R_RESULT = 1
R_PROBE = 2
R_NOTIFY = 3
R_NS = 4

ERRS = ["", "ENOENT", "ENOTDIR", "EISDIR", "EEXIST", "EROFS", "EACCES", "ENOTEMPTY", "EINVAL"]
ERR_CODE = {e: i for i, e in enumerate(ERRS) if e}
ZIP_MIN = 512

HDR = struct.Struct(">BBI")
RHDR = struct.Struct(">BBBId")
QQ = struct.Struct(">qq")
Q = struct.Struct(">q")
H = struct.Struct(">H")
STAT = struct.Struct(">BQ")


def read_path(data, pos):
    n = H.unpack_from(data, pos)[0]
    pos += 2
    return data[pos : pos + n].decode(), pos + n


class Server:
    def __init__(self, env, fs, config):
        self.env = env
        self.fs = fs
        self.config = config
        self.level = 1  # уровень zlib, на медленном канале выше
        self.incompressible = set()

    def on_start(self):
        pass

    def reply(self, req, status, flags=0, body=b""):
        self.env.send(RHDR.pack(R_RESULT, flags, status, req, self.env.now()) + body)

    def on_message(self, data):
        kind, flags, req = HDR.unpack_from(data)
        if kind == M_PROBE:
            self.env.send(RHDR.pack(R_PROBE, 0, 0, req, self.env.now()))
            return
        if kind == M_PARAMS:
            inv_bw = struct.unpack_from(">dddd", data, HDR.size)[0]
            # на медленном канале байт дорог, сжимаем сильнее
            self.level = 6 if inv_bw > 1.0 / 2e6 else 1
            return
        if kind == M_NSREQ:
            self.send_namespace()
            return
        op = OPS[kind - 1]
        path, pos = read_path(data, HDR.size)
        try:
            flags_out, body = self.execute(op, path, data, pos, flags)
            status = 0
        except Exception as e:
            code = getattr(e, "code", None)
            if code is None:
                if flags & F_ASYNC:
                    return
                raise
            status = ERR_CODE.get(code, ERR_CODE["EINVAL"])
            flags_out, body = 0, b""
        if not flags & F_ASYNC:
            self.reply(req, status, flags_out, body)

    def execute(self, op, path, data, pos, flags):
        fs = self.fs
        if op == "stat":
            st = fs.stat(path)
            return 0, STAT.pack(1 if st["type"] == "file" else 2, st["size"])
        if op == "listdir":
            body = "\x00".join(fs.listdir(path)).encode()
            if len(body) >= ZIP_MIN:
                z = zlib.compress(body, self.level)
                if len(z) < len(body):
                    return F_ZIP, z
            return 0, body
        if op == "read":
            offset, size = QQ.unpack_from(data, pos)
            out = fs.read(path, offset, size)
            return self.pack_data(path, out)
        if op == "create":
            fs.create(path)
        elif op == "write":
            offset = Q.unpack_from(data, pos)[0]
            payload = data[pos + 8 :]
            if flags & F_ZIP:
                payload = zlib.decompress(payload)
            fs.write(path, offset, payload)
            self.incompressible.discard(path)
        elif op == "truncate":
            fs.truncate(path, Q.unpack_from(data, pos)[0])
        elif op == "mkdir":
            fs.mkdir(path)
        elif op == "unlink":
            fs.unlink(path)
        elif op == "rename":
            dst = read_path(data, pos)[0]
            fs.rename(path, dst)
        elif op != "flush":
            raise ValueError(f"unknown op {op}")
        return 0, b""

    def pack_data(self, path, out):
        n = len(out)
        if n < ZIP_MIN or path in self.incompressible:
            return 0, out
        if n > 16384:
            sample = zlib.compress(out[:4096], 1)
            if len(sample) > 3900:
                self.incompressible.add(path)
                return 0, out
        z = zlib.compress(out, self.level)
        if len(z) < n * 0.95:
            return F_ZIP, z
        self.incompressible.add(path)
        return 0, out

    def send_namespace(self):
        fs = self.fs
        out = {}
        stack = [""]
        while stack:
            d = stack.pop()
            try:
                names = fs.listdir(d)
            except Exception as e:
                if getattr(e, "code", None) is None:
                    raise
                continue
            files, dirs, links, other = [], [], [], []
            for n in names:
                p = d + "/" + n if d else n
                try:
                    st = fs.stat(p)
                except Exception as e:
                    code = getattr(e, "code", None)
                    if code is None:
                        raise
                    (links if code == "EACCES" else other).append(n)
                    continue
                if st["type"] == "dir":
                    dirs.append(n)
                    stack.append(p)
                else:
                    files.append(n)
            out[d] = [files, dirs, links, other]
        raw = json.dumps(out, separators=(",", ":")).encode()
        self.env.send(RHDR.pack(R_NS, 0, 0, 0, self.env.now()) + zlib.compress(raw, 6))

    def on_local_change(self, path):
        self.incompressible.discard(path)
        self.env.send(RHDR.pack(R_NOTIFY, 0, 0, 0, self.env.now()) + path.encode())

    def on_timer(self, key):
        pass
