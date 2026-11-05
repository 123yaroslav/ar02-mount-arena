"""Файловая система в памяти и фасад к ней для серверной части протокола.

Одна и та же реализация служит и папкой пользователя (в процессе сервера), и эталоном
(в процессе раннера). Поэтому семантика ошибок у них совпадает по построению.

Пути относительные, через "/". Абсолютный путь, выход через ".." выше корня и любая
символьная ссылка дают EACCES: в деревьях есть только ссылки, ведущие наружу.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator

ERRORS = {"ENOENT", "ENOTDIR", "EISDIR", "EEXIST", "EROFS", "EACCES", "ENOTEMPTY", "EINVAL"}


class FSError(Exception):
    def __init__(self, code: str):
        assert code in ERRORS, code
        super().__init__(code)
        self.code = code


class Node:
    __slots__ = ("kind", "data", "children", "mtime", "target")

    def __init__(self, kind: str, mtime: float = 0.0):
        self.kind = kind  # "file" | "dir" | "link"
        self.data = bytearray() if kind == "file" else None
        self.children: dict[str, Node] | None = {} if kind == "dir" else None
        self.mtime = mtime
        self.target = ""


def split_path(path: str) -> list[str]:
    """Нормализует путь и возвращает компоненты. Выход за корень дает EACCES."""
    if not isinstance(path, str) or path.startswith("/") or "\x00" in path:
        raise FSError("EACCES")
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise FSError("EACCES")
            parts.pop()
        else:
            parts.append(part)
    return parts


def digest(data: bytes | bytearray) -> str:
    return hashlib.blake2b(bytes(data), digest_size=16).hexdigest()


class MemFS:
    def __init__(self) -> None:
        self.root = Node("dir")

    # навигация
    def _walk(self, parts: list[str]) -> Node:
        node = self.root
        for part in parts:
            if node.kind == "link":
                raise FSError("EACCES")
            if node.kind != "dir":
                raise FSError("ENOTDIR")
            child = node.children.get(part)  # type: ignore[union-attr]
            if child is None:
                raise FSError("ENOENT")
            node = child
        if node.kind == "link":
            raise FSError("EACCES")
        return node

    def node(self, path: str) -> Node:
        return self._walk(split_path(path))

    def _parent(self, path: str) -> tuple[Node, str]:
        parts = split_path(path)
        if not parts:
            raise FSError("EINVAL")
        parent = self._walk(parts[:-1])
        if parent.kind != "dir":
            raise FSError("ENOTDIR")
        name = parts[-1]
        existing = parent.children.get(name)  # type: ignore[union-attr]
        if existing is not None and existing.kind == "link":
            raise FSError("EACCES")
        return parent, name

    # чтение
    def stat(self, path: str) -> dict:
        node = self.node(path)
        size = len(node.data) if node.kind == "file" else 0  # type: ignore[arg-type]
        return {"type": node.kind, "size": size, "mtime": node.mtime}

    def listdir(self, path: str) -> list[str]:
        node = self.node(path)
        if node.kind != "dir":
            raise FSError("ENOTDIR")
        return sorted(node.children)  # type: ignore[arg-type]

    def read(self, path: str, offset: int, size: int) -> bytes:
        node = self.node(path)
        if node.kind == "dir":
            raise FSError("EISDIR")
        if offset < 0 or size < 0:
            raise FSError("EINVAL")
        return bytes(node.data[offset : offset + size])  # type: ignore[index]

    # изменения
    def create(self, path: str, now: float) -> None:
        parent, name = self._parent(path)
        node = parent.children.get(name)  # type: ignore[union-attr]
        if node is None:
            parent.children[name] = Node("file", now)  # type: ignore[index]
            parent.mtime = now
        elif node.kind == "dir":
            raise FSError("EISDIR")
        else:
            node.data = bytearray()
            node.mtime = now

    def write(self, path: str, offset: int, data: bytes, now: float) -> None:
        node = self.node(path)
        if node.kind == "dir":
            raise FSError("EISDIR")
        if offset < 0:
            raise FSError("EINVAL")
        buf = node.data
        if offset > len(buf):  # type: ignore[arg-type]
            buf.extend(b"\x00" * (offset - len(buf)))  # type: ignore[union-attr, arg-type]
        buf[offset : offset + len(data)] = data  # type: ignore[index]
        node.mtime = now

    def truncate(self, path: str, size: int, now: float) -> None:
        node = self.node(path)
        if node.kind == "dir":
            raise FSError("EISDIR")
        if size < 0:
            raise FSError("EINVAL")
        buf = node.data
        if size <= len(buf):  # type: ignore[arg-type]
            del buf[size:]  # type: ignore[union-attr]
        else:
            buf.extend(b"\x00" * (size - len(buf)))  # type: ignore[union-attr, arg-type]
        node.mtime = now

    def mkdir(self, path: str, now: float) -> None:
        parent, name = self._parent(path)
        if name in parent.children:  # type: ignore[operator]
            raise FSError("EEXIST")
        parent.children[name] = Node("dir", now)  # type: ignore[index]
        parent.mtime = now

    def unlink(self, path: str, now: float) -> None:
        parent, name = self._parent(path)
        node = parent.children.get(name)  # type: ignore[union-attr]
        if node is None:
            raise FSError("ENOENT")
        if node.kind == "dir":
            raise FSError("EISDIR")
        del parent.children[name]  # type: ignore[union-attr]
        parent.mtime = now

    def rename(self, src: str, dst: str, now: float) -> None:
        sparent, sname = self._parent(src)
        node = sparent.children.get(sname)  # type: ignore[union-attr]
        if node is None:
            raise FSError("ENOENT")
        dparent, dname = self._parent(dst)
        target = dparent.children.get(dname)  # type: ignore[union-attr]
        if target is not None and target.kind == "dir":
            raise FSError("EISDIR")
        if node.kind == "dir":
            raise FSError("EINVAL")  # в сценариях переименуются только файлы
        del sparent.children[sname]  # type: ignore[union-attr]
        dparent.children[dname] = node  # type: ignore[index]
        sparent.mtime = now
        dparent.mtime = now

    def add_link(self, path: str, target: str) -> None:
        parent, name = self._parent(path)
        link = Node("link")
        link.target = target
        parent.children[name] = link  # type: ignore[index]

    # состояние для проверок
    def state(self, path: str) -> tuple:
        try:
            node = self.node(path)
        except FSError as e:
            return ("absent",) if e.code == "ENOENT" else ("error", e.code)
        if node.kind == "dir":
            return ("dir", tuple(sorted(node.children)))  # type: ignore[arg-type]
        return ("file", bytes(node.data))  # type: ignore[arg-type]

    def iter_nodes(self) -> Iterator[tuple[str, Node]]:
        stack: list[tuple[str, Node]] = [("", self.root)]
        while stack:
            prefix, node = stack.pop()
            for name in sorted(node.children or {}):
                child = node.children[name]  # type: ignore[index]
                path = f"{prefix}{name}"
                yield path, child
                if child.kind == "dir":
                    stack.append((path + "/", child))

    def snapshot(self) -> dict[str, str]:
        """Компактное состояние всего дерева: путь -> тип и хеш содержимого."""
        out = {}
        for path, node in self.iter_nodes():
            if node.kind == "file":
                out[path] = "file:" + digest(node.data)  # type: ignore[arg-type]
            else:
                out[path] = node.kind
        return out


class LocalFS:
    """Фасад папки пользователя, который видит серверная часть протокола.

    Фасад замороженный. Он не дает выйти за папку и писать в режиме read-only,
    поэтому эти гейты держатся по построению, а не на коде кандидата.
    """

    def __init__(self, memfs: MemFS, read_only: bool, clock: Callable[[], float]):
        self._fs = memfs
        self._ro = read_only
        self._clock = clock

    @property
    def read_only(self) -> bool:
        return self._ro

    def _check_writable(self) -> None:
        if self._ro:
            raise FSError("EROFS")

    def stat(self, path: str) -> dict:
        return self._fs.stat(path)

    def listdir(self, path: str) -> list[str]:
        return self._fs.listdir(path)

    def read(self, path: str, offset: int = 0, size: int = 1 << 62) -> bytes:
        return self._fs.read(path, offset, size)

    def create(self, path: str) -> None:
        self._check_writable()
        self._fs.create(path, self._clock())

    def write(self, path: str, offset: int, data: bytes) -> None:
        self._check_writable()
        self._fs.write(path, offset, bytes(data), self._clock())

    def truncate(self, path: str, size: int) -> None:
        self._check_writable()
        self._fs.truncate(path, size, self._clock())

    def mkdir(self, path: str) -> None:
        self._check_writable()
        self._fs.mkdir(path, self._clock())

    def unlink(self, path: str) -> None:
        self._check_writable()
        self._fs.unlink(path, self._clock())

    def rename(self, src: str, dst: str) -> None:
        self._check_writable()
        self._fs.rename(src, dst, self._clock())


def apply_user_edit(fs: MemFS, edit: dict, now: float) -> list[str]:
    """Правка пользователя в редакторе. Обходит read-only, потому что это его папка."""
    kind, path = edit["kind"], edit["path"]
    if kind == "write":
        fs.create(path, now)
        fs.write(path, 0, edit["data"], now)
    elif kind == "unlink":
        fs.unlink(path, now)
    elif kind == "mkdir":
        fs.mkdir(path, now)
    else:
        raise ValueError(kind)
    return [path]
