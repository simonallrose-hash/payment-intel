# payment-intel — Модуль C: база платёжной инфраструктуры сайтов

B2B-датасет «домен e-commerce × PSP × способы оплаты × платформа» по ТЗ v1.5
(08.10.2026). Репозиторий реализует этапы 0–3; этапы 4–5 (модуль A UI/API,
фаза 2 «C2») не реализуются, оставлены только точки расширения
(`feature_c2_enabled`, пакет `src/payintel/c2/`).

Текущее состояние: **этап 1 — discovery и лёгкий скан** поверх фундамента
этапа 0 (схема данных, миграции, справочники, правила детекции, gold set и
`make eval`, инфраструктура и CI): импорт источников доменов с lineage, DNS
через собственный Unbound, классификатор e-commerce, планировщик с очередью
`SKIP LOCKED`, лёгкий сканер (robots → главная → товары → корзина) с
детекцией платформы/PSP/страны, артефактами в S3 и наблюдениями в ClickHouse.

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
| 4 | `payintel seed` | 135 PSP, 50 способов оплаты, 21 платформа, 20 вертикалей, 700 правил детекции |

Проверка руками:

```bash
uv run payintel rules-check                      # валидация YAML правил без БД
uv run payintel flags list                        # feature_c2_enabled = off, allow_* = on
uv run payintel audit verify                      # хеш-цепочка audit_log
uv run payintel gold import tests/fixtures/gold/sample_labels.csv --labeled-by me
uv run payintel gold import-findings tests/fixtures/gold/sample_findings.csv
uv run payintel eval --report eval-report.json   # precision/recall/F1 по сущностям и по правилам
```

Этап 1 руками (после `make dev`):

```bash
uv run payintel discovery import tests/fixtures/sources/tranco_sample.csv --source tranco
uv run payintel discovery import my-domains.csv              # ручной список (колонка domain)
uv run payintel discovery resolve --limit 1000               # DNS через Unbound :5335, parking по NS/CNAME
uv run payintel scheduler plan                               # scan_plan + приоритеты (FR-SC-01)
uv run payintel scheduler prioritize shop.example.de --actor me   # в начало очереди, с аудитом
uv run payintel worker-light --once --limit 50 --concurrency 50   # один пакет лёгких сканов
make bench-light                                             # NFR-P-01 на локальном фикстурном сервере
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
| `make worker-light` / `make worker-checkout` | цикл лёгкого сканера / сканера чекаута против dev-окружения |
| `make test-e2e` | e2e-проходы Chromium по симулятору магазинов (`PAYINTEL_TEST_TRAP_RUNS=1000` для AC-04) |
| `make bench-light` | нагрузочный прогон NFR-P-01 (`BENCH_DOMAINS`, `BENCH_CONCURRENCY`), не в CI |
| `make api` | API + портал + админка на http://127.0.0.1:8000 против dev-окружения |
| `make notifier` / `make exporter` | один цикл алертов / экспортов (`--once`) против dev-окружения |
| `make test-stage3` | тесты API, портала, экспортов, алертов, compliance, отчётов и контракта OpenAPI |

## Структура

```text
alembic/            миграции Postgres (0001 схема … 0005 отчёты по организациям)
clickhouse/         нумерованные идемпотентные SQL-миграции ClickHouse (раздел 5.2 ТЗ)
docker/             Dockerfile приложения, конфиги Unbound и Caddy
docs/               runbook, methodology, api, capacity, adr/, legal/
reference/          справочники YAML + JSON-схемы (PSP, методы, платформы, вертикали, stop_reasons)
rules/              правила детекции YAML (providers/, methods/, platforms/) + схема
src/payintel/
  core/             settings, logging, errors, db, ch, s3, flags, crypto, clock, audit, models/
  detect/           правила (rules), движок сигналов (engine), скоринг, страна (FR-DT-*)
  discovery/        нормализация, PSL, источники, ingest с lineage, DNS, parking, классификатор
  scheduler/        приоритет, планы, очередь SKIP LOCKED, backoff, politeness (FR-SC-*)
  crawl/            egress-фильтр, PII-санитайзер; light/: robots, fetcher, HTML, артефакты, воркер
                    checkout/: browser (Playwright), guardrails/ (политика кликов, GuardedPage, инжекция),
                    адаптеры, walker, capture, blocking, stops, accounts, payment_fill, worker (FR-CW-*)
  history/          буфер наблюдений ClickHouse, материализация store_*, differ «два подряд», read (FR-HI-*)
  quality/          gold set, импорт находок, eval, дашборд (FR-QA-03), разбор стопов (FR-QA-06)
  c2/               пустой пакет фазы 2, импорт только при feature_c2_enabled
  entitlements/     Principal/Grant, resolve_grant, сегменты, lineage guard, rate limit и квоты (FR-API-06/07)
  api/              FastAPI: v1/ (stores, changes, stats, watchlists, webhooks, exports, usage), auth/ (ключи,
                    пароли argon2id, TOTP, сессии, CSRF), schemas/ (c1_basic, c1_full, internal), portal/, admin/,
                    public/ (bot, optout, dsar), templates/ (Jinja2), static/app.css, problems (RFC 9457)
  compliance/       KYC-досье и решения, жизненный цикл организации, контракты/entitlements, opt-out, DSAR
  exports/          задания, builder (Parquet/CSV/агрегаты), watermark, canary, worker (FR-EX-*)
  alerts/           watchlists, правила, matcher, webhook (HMAC) и telegram, delivery с ретраями, worker (FR-AL-*)
  reports/          агрегаты с подавлением ячеек, Уилсон, XLSX/CSV с листом методологии (FR-RP-*)
  detect/admin.py   версии правил из админки, overlay на YAML, предпросмотр на gold set (FR-ADM-02)
  c2/               пустой пакет фазы 2, импорт только при feature_c2_enabled
scripts/            check_coverage.py, bench_light.py (NFR-P-01)
tests/              unit/ (без контейнеров), integration/ (testcontainers), e2e/ (Chromium + симулятор магазинов),
                    stage3/ (API, портал, экспорты, алерты, compliance, отчёты, контракт OpenAPI)
tests/fixtures/shops/   37 конфигураций симулятора: адаптеры, ловушка, блокировки, каждый stop_step
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
- `tests/integration` — Postgres, ClickHouse, S3 и Redis через testcontainers. Можно
  подставить внешние сервисы: `PAYINTEL_TEST_PG_DSN`, `PAYINTEL_TEST_CH_URL`
  (+ `_CH_USER/_CH_PASSWORD/_CH_DATABASE`), `PAYINTEL_TEST_S3_ENDPOINT`
  (+ `_S3_ACCESS_KEY/_S3_SECRET_KEY`), `PAYINTEL_TEST_REDIS_URL`. Так же работает CI.
- Лёгкий сканер тестируется против локального HTTP-сервера с фикстурными
  магазинами (`tests/fixtures/shops/`), транспорт httpx перенаправляется на
  127.0.0.1; politeness-лимитер идёт по виртуальным часам, так что 1 rps
  проверяется без ожиданий.
- `tests/e2e` — настоящий Chromium (Playwright, `uv run playwright install --with-deps chromium`)
  против симулятора магазинов `tests/e2e/shopsim.py` (локальный HTTP-сервер; браузер попадает
  на него через перехват `context.route`, DNS не нужен). Конфигурации в
  `tests/fixtures/shops/*/shop.json`. Магазин-ловушка: `PAYINTEL_TEST_TRAP_RUNS` (100 по
  умолчанию, 1000 для AC-04), `PAYINTEL_TEST_TRAP_CONCURRENCY` (8).
- `tests/stage3` — приложение FastAPI поднимается в транзакции теста (TestClient, без сети);
  TOTP проходится `pyotp` по виртуальным часам, webhook-получатель — `httpx.MockTransport`,
  DNS/HTTP-доказательства opt-out — фейки. Контрактные тесты — schemathesis по `/openapi.json`
  (derandomize, без интернета).
- Время — через `core/clock.py` (`FixedClock` в тестах), случайность не используется.

## Правила безопасности (неизменяемые)

Сканер никогда не нажимает кнопки оплаты и заказа, не вводит реальные карточные
данные, не решает CAPTCHA, не маскируется под браузер человека, не меняет IP и
соблюдает `robots.txt`. Поля оплаты заполняются только значениями из
`reference/test_payment_values.yaml` (ADR-0003) и только когда шаг оплаты не
раскрыл способы сам; каждое действие прохода идёт через `GuardedPage`
(ADR-0013), проверено на магазине-ловушке ×1 000 (AC-04). Поля C1 (AS-23) не
попадают в клиентские ответы и выгрузки (отдельные схемы на профиль, AC-07).
Клиентский доступ: только по одобренному KYC и контракту в сроке, ключи
показываются один раз (HMAC + pepper в БД), обязательный TOTP, CSP без
inline, CSRF, lockout; экспорты с водяным знаком и канарейками; агрегаты
только по ячейкам ≥ 30 магазинов. Подробно: `docs/methodology.md`,
`docs/api.md`, `docs/adr/`.

## Документация

`docs/runbook.md` (эксплуатация), `docs/methodology.md` (как считаются
находки и качество), `docs/api.md` (контракт API), `docs/capacity.md`
(ёмкость и серверы), `docs/adr/` (решения по неясностям ТЗ), `docs/legal/`
(каркасы LIA и RoPA для юриста).
