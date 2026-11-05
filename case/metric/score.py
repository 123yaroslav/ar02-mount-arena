"""Метрика одного экземпляра AR-02.

stdin  {"example": {"id", "input", "reference"}, "answer": "<JSON-отчет симулятора>"}
stdout {"score": число, "statistics": {...}}

Экземпляр валиден, если в отчете нет нарушений гейтов, число операций совпадает с
эталонным и итоговое состояние папки совпадает с тем, что получил baseline.
Оценка валидного экземпляра это log2(время baseline / время кандидата). Ноль значит
так же быстро, как baseline, единица значит вдвое быстрее. Невалидный экземпляр
получает штраф, а агрегатор обнуляет весь набор.
"""

import json
import math
import sys

FORMAT = "ar02-v1"
REPORT_VERSION = "mountsim-v1"
PENALTY = -10.0
MIN_TOTAL = 0.001  # пол на время экземпляра, с


def evaluate(example: dict, answer: str) -> dict:
    ref = json.loads(example["reference"])
    spec = json.loads(example["input"])
    stats = {
        "valid": False,
        "kind": spec["scenario"]["kind"],
        "rtt_ms": spec["profile"]["rtt_ms"],
        "loss": spec["profile"]["loss"],
        "baseline_total": ref["baseline_total"],
    }
    if ref.get("format") != FORMAT:
        raise ValueError("неизвестный формат эталона")
    try:
        report = json.loads(answer)
    except (TypeError, ValueError):
        stats["reason"] = "ответ не JSON"
        return {"score": PENALTY, "statistics": stats}
    if not isinstance(report, dict) or report.get("version") != REPORT_VERSION:
        stats["reason"] = "отчет не того формата"
        return {"score": PENALTY, "statistics": stats}
    if report.get("violations") or report.get("ok") is not True:
        stats["reason"] = "нарушение гейта: " + str((report.get("violations") or ["ok=false"])[0])[:500]
        return {"score": PENALTY, "statistics": stats}
    total = report.get("total")
    if not isinstance(total, (int, float)) or not math.isfinite(total) or total < 0:
        stats["reason"] = "нет корректного времени"
        return {"score": PENALTY, "statistics": stats}
    if report.get("ops") != ref["ops"]:
        stats["reason"] = f"выполнено {report.get('ops')} операций вместо {ref['ops']}"
        return {"score": PENALTY, "statistics": stats}
    if report.get("final_digest") != ref["final_digest"]:
        stats["reason"] = "итоговое состояние папки не совпало с эталоном"
        return {"score": PENALTY, "statistics": stats}
    total = max(MIN_TOTAL, float(total))
    stats.update({"valid": True, "total": total, "fs_wait": report.get("fs_wait"), "cpu": report.get("cpu_client", 0) + report.get("cpu_server", 0)})
    return {"score": math.log2(ref["baseline_total"] / total), "statistics": stats}


def main() -> None:
    payload = json.load(sys.stdin)
    print(json.dumps(evaluate(payload["example"], payload["answer"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
