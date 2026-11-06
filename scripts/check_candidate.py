"""Проверка области изменений кандидата до оценки.

Кандидат может менять только protocol/client.py, protocol/server.py и NOTES.md.
README.md допускается, он описывает способ и в оценке не участвует.
solve.py должен совпадать с baseline байт в байт. Код протокола не должен трогать
раннер, процессы, сокеты и файлы сандбокса. Это дешевая проверка, ревьюер смотрит дифф.

uv run python scripts/check_candidate.py case/ways/<candidate>
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "case" / "ways" / "baseline"
ALLOWED = {"solve.py", "NOTES.md", "README.md", "protocol/__init__.py", "protocol/client.py", "protocol/server.py"}
FORBIDDEN = [
    r"\bmountsim\b", r"\bsubprocess\b", r"\bmultiprocessing\b", r"\bthreading\b", r"\bctypes\b", r"\bsocket\b",
    r"\bimportlib\b", r"__import__", r"\bgc\b", r"\binspect\b", r"sys\.modules", r"\bos\.", r"\bopen\(",
    r"\bsignal\b", r"\bresource\b", r"\btempfile\b", r"\bpathlib\b", r"\bshutil\b", r"\beval\(", r"\bexec\(",
]


def check(cand: Path) -> list[str]:
    problems = []
    files = {str(p.relative_to(cand)) for p in cand.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    for extra in sorted(files - ALLOWED):
        problems.append(f"лишний файл {extra}")
    for need in ["solve.py", "protocol/__init__.py", "protocol/client.py", "protocol/server.py"]:
        if need not in files:
            problems.append(f"нет файла {need}")
    if "solve.py" in files:
        if hashlib.sha256((cand / "solve.py").read_bytes()).digest() != hashlib.sha256((BASELINE / "solve.py").read_bytes()).digest():
            problems.append("solve.py отличается от фиксированного запускателя")
    for name in ["protocol/client.py", "protocol/server.py", "protocol/__init__.py"]:
        if name not in files:
            continue
        text = (cand / name).read_text()
        for pattern in FORBIDDEN:
            if re.search(pattern, text):
                problems.append(f"{name}: запрещенная конструкция {pattern}")
    return problems


def main() -> None:
    problems = check(Path(sys.argv[1]))
    for p in problems:
        print(p)
    print("ok" if not problems else f"нарушений {len(problems)}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
