"""Агрегатор набора AR-02.

stdin  {"results": [{"example_id", "answer", "score", "statistics", "resources"}, ...]}
stdout {"score": среднее геометрическое ускорения относительно baseline}

Если хотя бы один экземпляр невалиден, весь набор получает 0. Корректность важнее
скорости, и обменять одно нарушение на ускорение остальных нельзя.
"""

import json
import sys


def aggregate(results: list[dict]) -> dict:
    if not results:
        raise ValueError("пустой набор")
    if any(not (r.get("statistics") or {}).get("valid") for r in results):
        return {"score": 0.0}
    scores = [float(r["score"]) for r in results]
    return {"score": 2 ** (sum(scores) / len(scores))}


def main() -> None:
    print(json.dumps(aggregate(json.load(sys.stdin)["results"])))


if __name__ == "__main__":
    main()
