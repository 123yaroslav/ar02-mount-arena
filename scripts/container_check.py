"""Прогон solve.py так, как его запускает runner арены. Один новый контейнер на пример.

Параметры docker run взяты из evaluator/runner.py арены. Пользователь 10001, корень только
для чтения, без сети, 2 ГБ памяти, 2 CPU, 128 процессов, /tmp 128 МБ, Python 3.12,
кандидат в /candidate и симулятор в /assets только для чтения. Нужен Docker и образ
python:3.12-slim. Печатает код выхода, пик памяти, размер ответа и балл метрики.

uv run python scripts/container_check.py case/ways/classic --ids train-t02-grep-rtt200-loss1,train-t00-big_file-rtt200-loss1
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAP = (
    "import resource,subprocess,sys,json;"
    "p=subprocess.run([sys.executable,'-E','-s','/candidate/solve.py'],stdin=sys.stdin,capture_output=True,cwd='/tmp');"
    "r=resource.getrusage(resource.RUSAGE_CHILDREN);"
    "sys.stderr.write(json.dumps({'rc':p.returncode,'maxrss_mb':r.ru_maxrss/1024})+'\\n');"
    "sys.stdout.buffer.write(p.stdout)"
)


def docker_cmd(cand: Path, assets: Path) -> list[str]:
    return [
        "docker", "run", "--rm", "-i", "--init", "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "2g", "--cpus", "2",
        "--network", "none", "--workdir", "/tmp",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001",
        "--mount", f"type=bind,src={cand},dst=/candidate,readonly",
        "--mount", f"type=bind,src={assets},dst=/assets,readonly",
        "python:3.12-slim", "python", "-I", "-c", WRAP,
    ]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("candidate", type=Path)
    p.add_argument("--data", type=Path, default=ROOT / "case" / "dataset" / "train.jsonl")
    p.add_argument("--ids", required=True)
    a = p.parse_args()
    rows = {json.loads(line)["id"]: json.loads(line) for line in a.data.read_text().splitlines()}
    ignore = shutil.ignore_patterns("__pycache__")
    with tempfile.TemporaryDirectory() as tmp:
        cand, assets = Path(tmp) / "candidate", Path(tmp) / "assets"
        shutil.copytree(a.candidate, cand, ignore=ignore)
        shutil.copytree(ROOT / "mountsim", assets / "mountsim", ignore=ignore)
        for ex_id in a.ids.split(","):
            ex = rows[ex_id]
            payload = json.dumps({"input": ex["input"], "assets_dir": "/assets", "prepare_timeout": 3600.0}, ensure_ascii=False, separators=(",", ":"))
            proc = subprocess.run(docker_cmd(cand, assets), input=payload.encode(), capture_output=True)
            meta = json.loads(proc.stderr.decode().strip().splitlines()[-1])
            out = json.loads(proc.stdout)
            assert set(out) == {"answer"} and isinstance(out["answer"], str)
            scored = subprocess.run([sys.executable, str(ROOT / "case" / "metric" / "score.py")],
                                    input=json.dumps({"example": ex, "answer": out["answer"]}), capture_output=True, text=True, check=True)
            res = json.loads(scored.stdout)
            print(json.dumps({"id": ex_id, "exit_code": meta["rc"], "maxrss_mb": round(meta["maxrss_mb"]), "answer_bytes": len(proc.stdout),
                              "valid": res["statistics"]["valid"], "score": round(res["score"], 3)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
