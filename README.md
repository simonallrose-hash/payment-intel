# payment-intel — Модуль C: база платёжной инфраструктуры сайтов

B2B-датасет «домен e-commerce × PSP × способы оплаты × платформа» по ТЗ v1.5
(08.10.2026). Репозиторий реализует этапы 0–3; этапы 4–5 (модуль A UI/API,
фаза 2 «C2») не реализуются, оставлены только точки расширения
(`feature_c2_enabled`, пакет `src/payintel/c2/`).

Текущее состояние: **этап 0 — фундамент** (схема данных, миграции, справочники,
правила детекции, gold set и `make eval`, инфраструктура и CI).

## Запуск за 15 минут

Нужны: Docker с Compose v2, [uv](https://docs.astral.sh/uv/) ≥ 0.5, Python 3.12
(uv скачает его сам), `make`.

```bash
git clone <repo> payment-intel && cd payment-intel
cp .env.example .env          # для локальной разработки значения можно оставить пустыми
make dev                      # uv sync → compose up (Postgres, ClickHouse, Redis, MinIO, Unbound, Caddy) → migrate → seed
make eval                     # метрики по gold set (пока пустой: gate "not applicable", exit 0)
make test                     # полный прогон: unit + integration (testcontainers) + покрытие
```

Что происходит внутри `make dev`:

| Шаг | Команда | Результат |
| --- | --- | --- |
| 1 | `uv sync --all-groups --frozen` | `.venv` с закреплёнными версиями из `uv.lock` |
| 2 | `docker compose -f docker-compose.dev.yml up -d --wait` | Postgres 16 `:5432`, ClickHouse 24.8 `:8123`, Redis 7 `:6379`, MinIO `:9002` (консоль `:9001`), Unbound `:5335`, Caddy `:8080` |
| 3 | `payintel migrate` | Alembic → head (35 таблиц, партиции `usage_log`, append-only триггер `audit_log`); ClickHouse `0001…0007` |
| 4 | `payintel seed` | 45 PSP, 50 способов оплаты, 21 платформа, 20 вертикалей, 268 правил детекции |

Проверка руками:

```bash
uv run payintel rules-check                      # валидация YAML правил без БД
uv run payintel flags list                        # feature_c2_enabled = off, allow_* = on
uv run payintel audit verify                      # хеш-цепочка audit_log
uv run payintel gold import tests/fixtures/gold/sample_labels.csv --labeled-by me
uv run payintel gold import-findings tests/fixtures/gold/sample_findings.csv
uv run payintel eval --report eval-report.json   # precision/recall/F1 по сущностям и по правилам
```

Остановить и удалить данные: `make dev-down`.

## Команды Makefile

| Цель | Назначение |
| --- | --- |
| `make dev` / `make dev-down` | поднять / снести локальное окружение |
| `make lint` / `make fmt` | ruff format + ruff check (`E,F,I,B,UP,S,ASYNC,RUF`) |
| `make typecheck` | `mypy --strict` для `src/` |
| `make test` / `make test-unit` | pytest с покрытием и пороговой проверкой по пакетам (NFR-M-01) |
| `make migrate` / `make seed` | миграции Postgres + ClickHouse / загрузка справочников и правил |
| `make eval` | метрики качества, ненулевой код при precision PSP < 0.95 (FR-QA-02) |

## Структура

```text
alembic/            миграции Postgres (единственная 0001_initial_schema на этапе 0)
clickhouse/         нумерованные идемпотентные SQL-миграции ClickHouse (раздел 5.2 ТЗ)
docker/             Dockerfile приложения, конфиги Unbound и Caddy
docs/               runbook, methodology, api, capacity, adr/, legal/
reference/          справочники YAML + JSON-схемы (PSP, методы, платформы, вертикали, stop_reasons)
rules/              правила детекции YAML (providers/, methods/, platforms/) + схема
src/payintel/
  core/             settings, logging, errors, db, ch, s3, flags, crypto, clock, audit, models/
  detect/           загрузчик и валидатор правил (FR-DT-01, FR-NR-05)
  quality/          gold set, импорт находок, eval (FR-QA-01/02)
  c2/               пустой пакет фазы 2, импорт только при feature_c2_enabled
  discovery/ scheduler/ crawl/ history/ api/ entitlements/ exports/ alerts/ reports/ compliance/
                    пакеты этапов 1–3 (пока без кода)
tests/              unit/ (без контейнеров) и integration/ (testcontainers)
```

## Конфигурация

Один модуль настроек `src/payintel/core/settings.py` (pydantic-settings).
Переменные окружения с префиксом `PAYINTEL_` и разделителем `__` для вложенных
групп: `PAYINTEL_POSTGRES__DSN`, `PAYINTEL_CLICKHOUSE__URL`,
`PAYINTEL_SECRETS__ENCRYPTION_KEY`, `PAYINTEL_SCAN__MAX_REQUESTS_PER_SECOND_PER_HOST` и т. д.
Все лимиты и сроки из ТЗ заданы значениями по умолчанию и проверяются тестом
`tests/unit/test_settings_defaults.py` (NFR-M-02). Секреты только через `.env`
(NFR-S-06); `.env.example` содержит пустые значения и проверяется тестом.

## Тесты

- `tests/unit` — без сети и контейнеров; `pytest-socket` блокирует всё, кроме localhost.
- `tests/integration` — Postgres, ClickHouse и S3 через testcontainers. Можно
  подставить внешние сервисы: `PAYINTEL_TEST_PG_DSN`, `PAYINTEL_TEST_CH_URL`
  (+ `_CH_USER/_CH_PASSWORD/_CH_DATABASE`), `PAYINTEL_TEST_S3_ENDPOINT`
  (+ `_S3_ACCESS_KEY/_S3_SECRET_KEY`). Так же работает CI.
- Время — через `core/clock.py` (`FixedClock` в тестах), случайность не используется.

## Правила безопасности (неизменяемые)

Сканер никогда не нажимает кнопки оплаты и заказа, не вводит реальные карточные
данные, не решает CAPTCHA, не маскируется под браузер человека, не меняет IP и
соблюдает `robots.txt`. Поля оплаты заполняются только значениями из
`reference/test_payment_values.yaml` (этап 2, ADR-0003). Поля C1 (AS-23) не
попадают в клиентские ответы и выгрузки. Подробно: `docs/methodology.md`,
`docs/adr/`.

## Документация

`docs/runbook.md` (эксплуатация), `docs/methodology.md` (как считаются
находки и качество), `docs/api.md` (контракт API, этап 3), `docs/capacity.md`
(ёмкость и серверы), `docs/adr/` (решения по неясностям ТЗ), `docs/legal/`
(каркасы LIA и RoPA для юриста).
