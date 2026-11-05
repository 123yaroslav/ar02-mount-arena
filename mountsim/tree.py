"""Генератор деревьев файлов по распределениям, снятым с открытых репозиториев.

Форма дерева (пути, размеры, большой файл, ссылки наружу) задается сидом дерева.
Содержимое задается отдельным сидом, поэтому одно дерево можно наполнить разными
байтами. Генерация детерминирована в любом процессе: сиды строковые, обход сортированный.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .fs import MemFS

STATS_PATH = Path(__file__).with_name("data") / "repo_stats.json"
MIN_FILES = 60
MAX_FILES = 2000
MAX_DEPTH = 9
MAX_TEXT = 1 << 20
MAX_BINARY = 4 << 20
BIG_MIN = 4 << 20
BIG_MAX = 24 << 20
ALPHA = 8.0  # склонность заводить новый каталог, подобрана под долю каталогов в реальных репозиториях

BINARY_EXT = [".png", ".jpg", ".gif", ".ico", ".webp", ".woff2", ".ttf", ".jar", ".zip", ".gz", ".pdf", ".bin", ".so", ".class", ".pyc", ".wasm"]
DIR_WORDS = [
    "src", "lib", "test", "tests", "docs", "utils", "core", "api", "internal", "pkg", "cmd", "app", "models",
    "views", "components", "config", "scripts", "assets", "data", "handlers", "services", "common", "client",
    "server", "storage", "plugins", "examples", "fixtures", "migrations", "templates", "static", "public",
    "types", "hooks", "store", "cli", "net", "io", "db", "auth", "parser", "runtime", "build", "tools", "spec",
]
SYLLABLES = ["ka", "ro", "mi", "ne", "to", "sa", "li", "ve", "do", "pa", "zu", "ge", "ri", "fo", "ba", "ku", "te", "lo", "ma", "xi"]
TOKENS = (
    "def class return import from if else for while in not and or None True False self this const let var func "
    "type struct impl pub fn match case break continue try except finally raise throw new await async yield with "
    "public private static void int str bool float list dict map err nil ctx req res data value key name id config "
    "path file line count index result error log debug info warn user item node tree parse load save open close "
    "read write test assert expect should equal len append get set update delete create build run start stop init"
).split()


@lru_cache(maxsize=1)
def load_stats() -> dict:
    return json.loads(STATS_PATH.read_text())


def _qsample(rng: random.Random, q: list[float]) -> float:
    """Сэмпл из распределения, заданного квантилями, с интерполяцией в логарифмах."""
    u = rng.random() * (len(q) - 1)
    i = int(u)
    a, b = q[i] + 1.0, q[min(i + 1, len(q) - 1)] + 1.0
    return math.exp(math.log(a) + (math.log(b) - math.log(a)) * (u - i)) - 1.0


def _word(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(SYLLABLES) for _ in range(n))


@dataclass
class TreeInfo:
    fs: MemFS
    files: list[tuple[str, int, bool]] = field(default_factory=list)  # путь, размер, текст ли
    dirs: list[str] = field(default_factory=list)
    big_file: str = ""
    big_is_text: bool = True
    links: list[str] = field(default_factory=list)

    @property
    def text_files(self) -> list[str]:
        return [p for p, _, t in self.files if t and p != self.big_file]

    def size_of(self, path: str) -> int:
        return len(self.fs.node(path).data)  # type: ignore[arg-type]


def _shape(seed: int) -> dict:
    """Форма дерева без содержимого: каталоги, файлы с размерами, большой файл, ссылки."""
    stats = load_stats()
    rng = random.Random(f"tree-shape:{seed}")
    counts = stats["file_count_per_repo"]
    n = int(rng.choice(counts) * math.exp(rng.gauss(0, 0.25)))
    n = max(MIN_FILES, min(MAX_FILES, n))
    depth_items = sorted((int(d), p) for d, p in stats["depth_probability"].items() if int(d) <= MAX_DEPTH)
    depth_values = [d for d, _ in depth_items]
    depth_weights = [p for _, p in depth_items]
    text_ext = [e for e, _ in stats["top_extensions"] if e not in BINARY_EXT and e.startswith(".")]
    text_w = [w for e, w in stats["top_extensions"] if e not in BINARY_EXT and e.startswith(".")]

    dirs_by_depth: dict[int, list[str]] = {0: [""]}
    weight: dict[str, int] = {"": 1}
    names_in: dict[str, set[str]] = {"": set()}

    def unique(parent: str, make) -> str:
        while True:
            name = make()
            if name not in names_in[parent]:
                names_in[parent].add(name)
                return name

    def pick_dir(depth: int) -> str:
        if depth == 0:
            return ""
        existing = dirs_by_depth.get(depth, [])
        total = sum(weight[d] for d in existing)
        if not existing or rng.random() * (total + ALPHA) < ALPHA:
            parent = pick_dir(depth - 1)
            name = unique(parent, lambda: rng.choice(DIR_WORDS) if rng.random() < 0.6 else _word(rng, rng.randint(2, 4)))
            path = f"{parent}/{name}" if parent else name
            dirs_by_depth.setdefault(depth, []).append(path)
            weight[path] = 1
            weight[parent] += 1
            names_in[path] = set()
            return path
        return rng.choices(existing, weights=[weight[d] for d in existing])[0]

    files = []
    for _ in range(n):
        depth = rng.choices(depth_values, weights=depth_weights)[0]
        directory = pick_dir(depth)
        weight[directory] += 1
        is_text = rng.random() < stats["text_fraction"]
        ext = rng.choices(text_ext, weights=text_w)[0] if is_text else rng.choice(BINARY_EXT)
        name = unique(directory, lambda e=ext: _word(rng, rng.randint(2, 5)) + e)
        if is_text:
            size = int(min(MAX_TEXT, _qsample(rng, stats["size_text_quantiles"])))
        else:
            size = int(min(MAX_BINARY, _qsample(rng, stats["size_binary_quantiles"])))
        files.append((f"{directory}/{name}" if directory else name, size, is_text))

    big_dir = rng.choice(["data", "logs", "dist", ""])
    if big_dir and big_dir not in names_in[""]:
        names_in[""].add(big_dir)
        dirs_by_depth.setdefault(1, []).append(big_dir)
        names_in[big_dir] = set()
    big_is_text = rng.random() < 0.6
    big_name = unique(big_dir, lambda: _word(rng, 3) + (".log" if big_is_text else ".bin"))
    big_size = int(max(BIG_MIN, min(BIG_MAX, _qsample(rng, stats["largest_file_per_repo"]))))
    big_path = f"{big_dir}/{big_name}" if big_dir else big_name
    files.append((big_path, big_size, big_is_text))

    links = []
    for _ in range(rng.randint(1, 2)):
        parent = rng.choice(dirs_by_depth.get(1, [""]) + [""])
        name = unique(parent, lambda: rng.choice(["shared", "home", "secrets", "env", "cache"]) + "_link")
        links.append(f"{parent}/{name}" if parent else name)

    all_dirs = sorted(p for ds in dirs_by_depth.values() for p in ds if p)
    return {"files": files, "dirs": all_dirs, "big": big_path, "big_is_text": big_is_text, "links": links}


LOCAL_POOL = 120  # половин строк в словаре одного файла
COMMON_SHARE = 0.3  # доля шаблонных строк (скобки, импорты, end, return), подобрана по настоящему коду
BOILERPLATE = [
    "}", "end", "{", "]", ")", "", "    }", "        }", "    end", "  end", "return nil", "return err", "pass", "else {",
    "} else {", "if err != nil {", "    return err", "import os", "import sys", "from typing import Any", "package main",
    "#include <stdio.h>", "#include <stdlib.h>", "use std::fmt;", "};", "*/", "/**", " */", "<?php", "</div>", "<div>",
    "    @Override", "import java.util.List;", "require 'json'", "module.exports = {", "export default {", "const _ = require('lodash');",
]


class TextSource:
    """Источник текста, похожего на код.

    Строка склеивается из двух случайных половин из пулов фрагментов, поэтому строки
    редко повторяются целиком. Четверть строк берется из небольшого набора шаблонных
    строк, как скобки и импорты в настоящем коде. Параметры подобраны так, чтобы zlib
    и словарь строк сжимали текст примерно как настоящие репозитории
    (scripts/calibrate_text.py).
    """

    def __init__(self, rng: random.Random, pool_size: int = 4000):
        self.rng = rng

        def fragment(indent: bool) -> str:
            words = rng.choices(TOKENS, k=rng.randint(1, 5))
            if rng.random() < 0.5:
                words.append(_word(rng, rng.randint(2, 4)) + rng.choice(["", "_id", "Name", "(", ")", "()", ":", ","]))
            if rng.random() < 0.25:
                words.append(str(rng.randint(0, 9999)))
            return (" " * (4 * rng.randint(0, 3)) if indent else "") + " ".join(words)

        self.heads = [fragment(True) for _ in range(pool_size)]
        self.tails = [fragment(False) for _ in range(pool_size)]
        self.common = BOILERPLATE + [fragment(True) for _ in range(160)]

    def lines(self, n: int, local: int = 0) -> list[str]:
        """n строк. local > 0 берет половины из случайного подмножества пулов, как словарь одного файла."""
        rng = self.rng
        head_pool = rng.sample(self.heads, local) if local else self.heads
        tail_pool = rng.sample(self.tails, local) if local else self.tails
        heads = rng.choices(head_pool, k=n)
        tails = rng.choices(tail_pool, k=n)
        common = rng.choices(self.common, k=n)
        mask = rng.choices((False, True), weights=(1 - COMMON_SHARE, COMMON_SHARE), k=n)
        return [c if m else h + " " + t for h, t, c, m in zip(heads, tails, common, mask)]

    def text(self, size: int) -> bytes:
        if size <= 0:
            return b""
        local = LOCAL_POOL
        data = ("\n".join(self.lines(size // 28 + 2, local)) + "\n").encode()
        while len(data) < size:
            data += ("\n".join(self.lines(size // 28 + 2, local)) + "\n").encode()
        return data[:size]

    def log(self, size: int) -> bytes:
        rng = self.rng
        levels = ["INFO", "DEBUG", "WARN", "ERROR"]
        t = 1700000000
        out = bytearray()
        while len(out) < size:
            chunk = []
            for line in self.lines(2000):
                t += rng.randint(0, 3)
                chunk.append(f"{t} {rng.choice(levels)} {line.strip()}")
            out += ("\n".join(chunk) + "\n").encode()
        return bytes(out[:size])


def build_tree(spec: dict) -> TreeInfo:
    """spec = {"seed": int, "content_seed": int}"""
    shape = _shape(int(spec["seed"]))
    rng = random.Random(f"tree-content:{spec['seed']}:{spec['content_seed']}")
    source = TextSource(rng)
    fs = MemFS()
    for d in shape["dirs"]:
        fs.mkdir(d, 0.0)
    files = []
    for path, size, is_text in shape["files"]:
        if path == shape["big"]:
            data = source.log(size) if is_text else rng.randbytes(size)
        else:
            data = source.text(size) if is_text else rng.randbytes(size)
        fs.create(path, 0.0)
        fs.write(path, 0, data, 0.0)
        files.append((path, size, is_text))
    for link in shape["links"]:
        fs.add_link(link, "../" * (link.count("/") + 2) + ".ssh/id_rsa")
    return TreeInfo(
        fs=fs,
        files=files,
        dirs=shape["dirs"],
        big_file=shape["big"],
        big_is_text=shape["big_is_text"],
        links=shape["links"],
    )


def describe(seed: int) -> dict:
    """Короткое описание формы дерева для отчетов и обоснования размеров split."""
    shape = _shape(seed)
    sizes = [s for p, s, _ in shape["files"] if p != shape["big"]]
    depths = [p.count("/") for p, _, _ in shape["files"]]
    return {
        "seed": seed,
        "files": len(shape["files"]),
        "dirs": len(shape["dirs"]),
        "bytes_without_big": sum(sizes),
        "big_bytes": next(s for p, s, _ in shape["files"] if p == shape["big"]),
        "median_size": sorted(sizes)[len(sizes) // 2],
        "max_depth": max(depths),
    }
