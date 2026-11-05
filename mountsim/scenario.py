"""Сценарии работы агента с файлами.

Сценарий это последовательность шагов. Шаг "op" это одна операция ФС уровня FUSE,
агент ждет ответа. Шаг "think" это пауза, пока LLM думает между вызовами тулов.
Шаг "user" это правка пользователя в редакторе, она применяется сразу.

Виды сценариев.
walk          обход дерева со stat каждого элемента (find, ls -R)
grep          обход и чтение всех текстовых файлов (grep -r, rg)
small_reads   несколько команд, каждая читает группу соседних файлов
big_file      голова, хвост и полное последовательное чтение большого файла
writes        правки файлов, новые файлы и каталоги, дописывание лога
session       смешанная сессия агента с правками пользователя
"""

from __future__ import annotations

import random

from .tree import TextSource, TreeInfo, _word

CHUNK = 128 * 1024  # max_read у FUSE по умолчанию
KINDS = ["walk", "grep", "small_reads", "big_file", "writes", "session"]
READ_ONLY_KINDS = {"walk", "grep"}


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


class Builder:
    def __init__(self, tree: TreeInfo, rng: random.Random, read_only: bool):
        self.tree = tree
        self.rng = rng
        self.ro = read_only
        self.steps: list[dict] = []
        self.text = TextSource(rng, pool_size=3000)
        self.kids: dict[str, set[str]] = {"": set()}
        self.size: dict[str, int] = {}
        self.is_text: dict[str, bool] = {}
        for path, node in tree.fs.iter_nodes():
            self.kids.setdefault(_parent(path), set()).add(path.rsplit("/", 1)[-1])
            if node.kind == "dir":
                self.kids.setdefault(path, set())
            elif node.kind == "file":
                self.size[path] = len(node.data)  # type: ignore[arg-type]
        for path, _, is_text in tree.files:
            self.is_text[path] = is_text
        self.links = set(tree.links)
        self.agent_touched: set[str] = set()
        self.user_touched: set[str] = set()
        self.read_paths: list[str] = []

    # примитивы
    def think(self, lo: float = 2.0, hi: float = 8.0) -> None:
        self.steps.append({"think": round(self.rng.uniform(lo, hi), 3)})

    def op(self, **op) -> None:
        self.steps.append({"op": op})

    def files_in(self, d: str) -> list[str]:
        return sorted(p for p in (_join(d, n) for n in self.kids.get(d, ())) if p in self.size)

    def subdirs(self, d: str) -> list[str]:
        return sorted(p for p in (_join(d, n) for n in self.kids.get(d, ())) if p in self.kids)

    def subtree_files(self, d: str) -> int:
        return len(self.files_in(d)) + sum(self.subtree_files(s) for s in self.subdirs(d))

    def readable(self, path: str) -> bool:
        return self.is_text.get(path, True) and path != self.tree.big_file and path in self.size

    def read_file(self, path: str) -> None:
        self.op(op="stat", path=path)
        size = self.size.get(path, 0)
        offset = 0
        while True:
            self.op(op="read", path=path, offset=offset, size=CHUNK)
            offset += CHUNK
            if offset > size:
                break
        self.read_paths.append(path)

    def ls(self, d: str) -> None:
        self.op(op="listdir", path=d)
        for name in sorted(self.kids.get(d, ())):
            self.op(op="stat", path=_join(d, name))

    def write_file(self, path: str, size: int) -> None:
        data = self.text.text(size)
        self.op(op="create", path=path)
        for off in range(0, len(data), CHUNK):
            self.op(op="write", path=path, offset=off, data=data[off : off + CHUNK])
        if not self.ro:
            if path not in self.size:
                self.kids[_parent(path)].add(path.rsplit("/", 1)[-1])
            self.size[path] = len(data)
            self.is_text[path] = True
        self.agent_touched.add(path)

    def user_edit(self, path: str) -> None:
        size = max(64, int(self.size.get(path, 2000) * self.rng.uniform(0.8, 1.2)))
        data = self.text.text(size)
        self.steps.append({"user": {"kind": "write", "path": path, "data": data}})
        if path not in self.size:
            self.kids[_parent(path)].add(path.rsplit("/", 1)[-1])
        self.size[path] = len(data)
        self.is_text[path] = True
        self.user_touched.add(path)

    def pick_focus(self) -> str:
        dirs = [d for d in self.kids if self.files_in(d)]
        weights = [len(self.files_in(d)) for d in dirs]
        return self.rng.choices(dirs, weights=weights)[0]

    def candidates_near(self, focus: str) -> list[str]:
        near = self.files_in(focus)
        parent = _parent(focus) if focus else ""
        near += self.files_in(parent) if focus else []
        for s in self.subdirs(focus):
            near += self.files_in(s)
        return [p for p in dict.fromkeys(near) if self.readable(p)]

    def untouched_text(self) -> list[str]:
        return [p for p in self.size if self.readable(p) and p not in self.agent_touched and p not in self.user_touched]

    def negative_stats(self, focus: str, k: int) -> None:
        for _ in range(k):
            name = _word(self.rng, 3) + self.rng.choice([".py", ".so", "/__init__.py", ".pyc", ".js", ".json"])
            path = _join(focus, name)
            if path not in self.size and path not in self.kids:
                self.op(op="stat", path=path)

    # виды сценариев
    def walk(self, d: str = "") -> None:
        if d == "":
            self.think(2, 6)
        self.op(op="listdir", path=d)
        for name in sorted(self.kids.get(d, ())):
            path = _join(d, name)
            self.op(op="stat", path=path)
            if path in self.kids:
                self.walk(path)

    def grep(self, d: str = "", edits: list[str] | None = None) -> None:
        if d == "":
            self.think(2, 6)
            pool = self.untouched_text()
            edits = self.rng.sample(pool, k=min(2, len(pool)))
            self._edit_queue = list(edits)
        self.op(op="listdir", path=d)
        for name in sorted(self.kids.get(d, ())):
            path = _join(d, name)
            if path in self.kids:
                self.grep(path, edits)
            elif self.readable(path):
                if self._edit_queue and self.rng.random() < 0.01:
                    self.user_edit(self._edit_queue.pop())
                self.read_file(path)

    def small_reads(self) -> None:
        self.think(2, 6)
        edited: list[str] = []
        for i in range(self.rng.randint(4, 8)):
            if i > 0:
                if edited and self.rng.random() < 0.7:
                    self.read_file(edited.pop())
                self.think(2, 8)
            focus = self.pick_focus()
            near = self.candidates_near(focus)
            for path in self.rng.sample(near, k=min(len(near), self.rng.randint(3, 15))):
                self.read_file(path)
            self.negative_stats(focus, self.rng.randint(1, 3))
            if self.rng.random() < 0.4 and self.read_paths:
                target = self.rng.choice(self.read_paths)
                if target not in self.user_touched:
                    self.think(1.5, 3)
                    self.user_edit(target)
                    edited.append(target)

    def big_file(self) -> None:
        big = self.tree.big_file
        size = self.size[big]
        parts = ["head", "tail", "full"]
        self.rng.shuffle(parts)
        for part in parts:
            self.think(2, 6)
            self.op(op="stat", path=big)
            if part == "head":
                self.op(op="read", path=big, offset=0, size=64 * 1024)
            elif part == "tail":
                self.op(op="read", path=big, offset=max(0, size - 64 * 1024), size=64 * 1024)
            else:
                for off in range(0, size + 1, CHUNK):
                    self.op(op="read", path=big, offset=off, size=CHUNK)

    def edit_existing(self) -> None:
        pool = self.untouched_text()
        if not pool:
            return
        path = self.rng.choice(pool)
        self.read_file(path)
        self.write_file(path, max(32, int(self.size[path] * self.rng.uniform(0.8, 1.25))))

    def new_files(self) -> None:
        base = self.pick_focus()
        if self.rng.random() < 0.35:
            new_dir = _join(base, _word(self.rng, 3))
            if new_dir not in self.kids and new_dir not in self.size:
                self.op(op="mkdir", path=new_dir)
                self.kids[base].add(new_dir.rsplit("/", 1)[-1])
                self.kids[new_dir] = set()
                base = new_dir
        for _ in range(self.rng.randint(1, 4)):
            path = _join(base, _word(self.rng, 4) + self.rng.choice([".py", ".ts", ".go", ".md", ".json"]))
            if path not in self.size and path not in self.kids:
                self.write_file(path, self.rng.randint(200, 40000))

    def append_log(self) -> None:
        path = "agent_run.log"
        if path not in self.size:
            self.write_file(path, 500)
        data = self.text.log(self.rng.randint(200, 4000))
        self.op(op="write", path=path, offset=self.size[path], data=data)
        self.size[path] += len(data)

    def rename_or_unlink(self) -> None:
        mine = sorted(p for p in self.agent_touched if p in self.size and p != "agent_run.log")
        if not mine:
            return
        path = self.rng.choice(mine)
        if self.rng.random() < 0.5:
            dst = _join(_parent(path), _word(self.rng, 4) + ".bak")
            if dst in self.size or dst in self.kids:
                return
            self.op(op="rename", path=path, dst=dst)
            self.kids[_parent(path)].discard(path.rsplit("/", 1)[-1])
            self.kids[_parent(dst)].add(dst.rsplit("/", 1)[-1])
            self.size[dst] = self.size.pop(path)
            self.agent_touched.add(dst)
        else:
            self.op(op="unlink", path=path)
            self.kids[_parent(path)].discard(path.rsplit("/", 1)[-1])
            self.size.pop(path)
        self.agent_touched.discard(path)

    def writes(self) -> None:
        self.think(2, 6)
        pending_reread: list[str] = []
        for i in range(self.rng.randint(4, 7)):
            if i > 0:
                self.think(2, 8)
            if pending_reread:
                self.read_file(pending_reread.pop())
            action = self.rng.choices(
                [self.edit_existing, self.new_files, self.append_log, self.rename_or_unlink], weights=[4, 3, 2, 1]
            )[0]
            action()
            if self.rng.random() < 0.5:
                self.edit_existing()
            if self.rng.random() < 0.4:
                pool = self.untouched_text()
                if pool:
                    target = self.rng.choice(pool)
                    self.think(1.5, 3)
                    self.user_edit(target)
                    pending_reread.append(target)

    def session(self) -> None:
        self.think(3, 8)
        self.ls("")
        top = [p for p in self.files_in("") if self.readable(p)]
        for path in self.rng.sample(top, k=min(2, len(top))):
            self.read_file(path)
        self.think(2, 8)
        dirs = [d for d in self.kids if d and 20 <= self.subtree_files(d) <= 400]
        if dirs:
            self.grep_subtree(self.rng.choice(dirs))
        self.think(2, 8)
        focus = self.pick_focus()
        self.ls(focus)
        near = self.candidates_near(focus)
        for path in self.rng.sample(near, k=min(len(near), self.rng.randint(4, 12))):
            self.read_file(path)
        self.think(2, 8)
        self.edit_existing()
        self.edit_existing()
        self.think(2, 8)
        self.run_tests(focus)
        target = None
        pool = [p for p in self.read_paths if p not in self.agent_touched and p not in self.user_touched]
        if pool:
            target = self.rng.choice(pool)
            self.think(1.5, 3)
            self.user_edit(target)
            new_path = _join(_parent(target), "notes_" + _word(self.rng, 3) + ".md")
            if new_path not in self.size:
                self.user_edit(new_path)
        self.think(2, 8)
        if target:
            self.read_file(target)
            self.ls(_parent(target))
        self.new_files()
        self.append_log()
        self.think(2, 8)
        self.run_tests(focus)

    def grep_subtree(self, d: str) -> None:
        self.op(op="listdir", path=d)
        for name in sorted(self.kids.get(d, ())):
            path = _join(d, name)
            if path in self.kids:
                self.grep_subtree(path)
            elif self.readable(path):
                self.read_file(path)

    def run_tests(self, focus: str) -> None:
        self.negative_stats(focus, self.rng.randint(5, 15))
        near = self.candidates_near(focus) or self.untouched_text()
        for path in self.rng.sample(near, k=min(len(near), self.rng.randint(5, 25))):
            self.op(op="stat", path=path)
            self.read_file(path)

    # пробы гейтов
    def escape_probe(self) -> None:
        self.think(1, 3)
        self.op(op="read", path="../.ssh/id_rsa", offset=0, size=4096)
        self.op(op="stat", path="src/../../outside.txt")
        for link in sorted(self.links):
            self.op(op="stat", path=link)
            self.op(op="read", path=link + "/x", offset=0, size=4096)

    def read_only_probe(self) -> None:
        pool = self.untouched_text()
        self.op(op="create", path="agent_output.txt")
        if pool:
            self.op(op="write", path=self.rng.choice(pool), offset=0, data=b"overwrite attempt\n")
        self.op(op="mkdir", path="new_dir_attempt")


def build_scenario(tree: TreeInfo, spec: dict, read_only: bool) -> list[dict]:
    """spec = {"kind": str, "seed": int}"""
    kind = spec["kind"]
    if kind not in KINDS:
        raise ValueError(f"unknown scenario kind {kind}")
    rng = random.Random(f"scenario:{kind}:{spec['seed']}")
    builder = Builder(tree, rng, read_only)
    builder._edit_queue = []
    getattr(builder, kind)()
    builder.escape_probe()
    if read_only:
        builder.read_only_probe()
    builder.op(op="flush")
    return builder.steps
