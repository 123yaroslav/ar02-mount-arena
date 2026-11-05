"""Фиксированный запускатель кандидата. Одинаков во всех кандидатах, менять нельзя.

Читает пример арены из stdin, гоняет замороженный симулятор mountsim из /assets
с протоколом этого кандидата и печатает отчет как answer. Любой сбой кандидата
превращается в отчет с нарушением, а не в падение процесса, чтобы не остановить
закрытую серию.
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    task = json.load(sys.stdin)
    assets = task.get("assets_dir") or os.environ.get("MOUNTSIM_HOME") or "/assets"
    sys.path.insert(0, os.environ.get("MOUNTSIM_HOME", assets))
    try:
        from mountsim.runner import run_instance

        report = run_instance(json.loads(task["input"]), HERE)
    except Exception as e:  # noqa: BLE001
        report = {"version": "launcher", "ok": False, "violations": [f"сбой запуска: {e!r}"]}
    print(json.dumps({"answer": json.dumps(report, ensure_ascii=False)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
