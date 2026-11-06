# AR-02. Протокол монтирования workdir, выращенный на арене

Тема AR-02 из [реестра курса](https://github.com/deeppavlov/agentic-course-itmo/blob/main/projects/arena-registry.md), задание B. Взята в порядке исключения, согласовано с kudep 05.11.2026.

LLM-агент работает в удаленной песочнице, а папка пользователя смонтирована туда как рабочая директория. Каждая файловая операция агента идет по сети. При RTT 200 мс обход репозитория из 2000 файлов с наивным протоколом занимает минуты. Нужно, чтобы типовые операции агента шли быстро при любой сети и чтобы при этом не ломалась согласованность. Агент видит правки пользователя, пользователь видит правки агента, ничего не теряется.

На арене эволюционирует код протокола, то есть клиент в песочнице и сервер у пользователя. Оценка идет в симуляторе сети с виртуальным временем, поэтому у метрики нет сетевого шума, а кандидат оценивается за минуту на ноутбуке.

## Итог в одной таблице

Ускорение относительно baseline, среднее геометрическое по всем экземплярам. Baseline равен 1,0. Любое нарушение согласованности дает 0.

| | Train | Public | Private |
|---|---|---|---|
| Baseline, один запрос на операцию | 1,00 | 1,00 | будет на ЧП3 |
| Классический протокол, кэш с уведомлениями, READDIRPLUS, файл целиком | 1,74 | 1,76 | будет на ЧП3 |
| Кандидаты r1 проверочной кампании на Opus | 0, одно нарушение | 2,2-2,4 | не допущены |
| Выращенное решение | основная кампания после ЧП1 | | будет на ЧП3 |

Цифры для симулятора v2 при 9 параллельных прогонах на одной машине. При тех же условиях повтор меняет итог на 0,1-0,8%. Смена параллельности сдвигает тяжелых по CPU кандидатов до 10%, поэтому условия замера зафиксированы, а для r1, чьи первые замеры шли при разной загрузке, дан диапазон ([ADR-0009](docs/adr/0009-cpu-time.md)). Версию v2 я сделал после проверочной кампании на Opus. Ее лучшие кандидаты опирались на детерминированную сеть и общие часы клиента и сервера, а генератор текста повторял строки чаще настоящего кода, подробности в [evolution.md](docs/evolution.md), [ADR-0007](docs/adr/0007-jitter-and-clocks.md) и [ADR-0008](docs/adr/0008-text-realism.md).

Основная кампания еще не запускалась. Лимит предложенных решений в ней 30, условия в [campaign.md](case/campaign/campaign.md).

## Как это устроено

```
mountsim/           замороженный симулятор: сеть, папка пользователя, сценарии агента, проверка ответов
case/               кейс арены: задача, данные, метрика, кампания, стартовый кандидат
case/ways/baseline  стартовый кандидат: solve.py (фиксирован) и protocol/client.py, protocol/server.py
case/ways/classic   классический протокол для сравнения, в эволюции не участвует
docs/domain.md      обзор домена и классических подходов
docs/arena.md       как устроена арена и чем моя установка от нее отличается
docs/adr/           решения по методологии, метрике, данным и изоляции
docs/evolution.md   разбор эволюции
docs/talk.md        план рассказа
results/            результаты оценок, журнал проверочной кампании, проверки (шум, абляция, контейнер)
scripts/            сбор статистики репозиториев, оценка кандидата, проверка области изменений
skill/              итоговый навык (к ЧП3)
```

Один экземпляр это дерево файлов, сценарий агента и профиль сети. Деревья генерируются по распределениям, снятым с 64 открытых репозиториев GitHub на 8 языках. Сценариев шесть. Это обход дерева, grep, чтение групп соседних файлов, большой файл, серия записей и смешанная сессия с паузами LLM и правками пользователя. Профилей восемь, это RTT 1, 20, 80 и 200 мс при потерях 0 и 1%.

## Как воспроизвести

```bash
uv sync
uv run pytest                                   # 47 тестов: сеть, папка, генератор, данные, метрика, 9 жульнических и сломанных кандидатов
uv run python case/dataset/generate.py --split train --seed 1 --trees 8 --reference --out case/dataset/train.jsonl
uv run python scripts/evaluate.py --candidate case/ways/baseline --data case/dataset/train.jsonl --jobs 9 --out results/v2/baseline/train.jsonl
uv run python scripts/evaluate.py --candidate case/ways/classic --data case/dataset/train.jsonl --jobs 9 --out results/v2/classic/train.jsonl
uv run python scripts/evaluate.py --candidate results/validation-opus/candidates/r1-c2 --data case/dataset/train.jsonl --jobs 9 --out results/v2/r1-c2/train.jsonl
uv run python scripts/curve.py results/validation-opus/journal.jsonl --limit 4 --key public_v1 --valid-key valid_v1 --out results/figures/curve_validation_v1.png
uv run python scripts/ablation.py results/validation-opus/candidates/r1-c1 --trees t00,t03 --jobs 9     # абляция джиттера и часов
uv run python scripts/container_check.py case/ways/classic --ids train-t02-grep-rtt200-loss1           # запуск как в runner арены, нужен Docker
uv run python scripts/plot_results.py --out results/figures/speedup_by_kind.png "классический протокол=results/v2/classic/train.jsonl"
uv run python scripts/plot_validation.py results/validation-opus/journal.jsonl --out results/figures/validation_v1_v2.png
uv run python scripts/plot_ablation.py results/checks/ablation.jsonl --out results/figures/ablation_r1c1.png
uv run python scripts/check_candidate.py case/ways/<кандидат>
uv run python scripts/collect_repo_stats.py --per-language 8 --out mountsim/data/repo_stats.json   # нужен gh
```

Оценка пишет рядом с результатами `*.summary.json` с баллом, версией метрики, хешами данных и кандидата и расходами. Public и private генерирует оператор тем же скриптом с сидом из файла вне репозитория (`--seed-file`), public оценивается той же командой с флагом `--quiet`. В арену кейс подключается так. Каталог `mountsim/` кладется в `/assets`, `case/` копируется в `cases/ar02` репозитория арены, кандидатом служит каталог из `case/ways/`.

## Модели

Решатель и метрика работают без LLM. Модель нужна только исследователям арены. Проверочный прогон 06.11.2026 шел на Claude Opus 5.5 (`claude-opus-5-5`), потому что доступ к моделям курса выдают с пятницы. Основная кампания пойдет на модели курса (DeepSeek 4.x), версия и Intelligence Index будут записаны в [campaign.md](case/campaign/campaign.md) в день запуска.
