# Runbook (этапы 0–1)

Эксплуатационные процедуры для того, что уже существует. Разделы для
сканера чекаута, API и алертов добавляются на этапах 2–3.

## 1. Серверы и раскладка (AS-17)

`docker-compose.yml` один, сервисы разнесены профилями:

| Профиль | Сервер | Сервисы |
| --- | --- | --- |
| `app-1` | приложение | Caddy (80/443), Postgres 16, Redis 7, `migrate` (одноразовый), далее api/portal (этап 3) |
| `crawl-1` | сканер | Unbound, `worker-light` (масштабируется `--scale worker-light=N`), воркер checkout (этап 2) |
| `data-1` | данные | ClickHouse 24.8, MinIO |

Запуск на сервере: `docker compose --profile app-1 up -d` (и аналогично для
остальных). Секреты берутся из `.env`, файл не хранится в репозитории
(NFR-S-06); контейнеры приложения запускаются non-root, read-only FS,
`cap_drop: ALL`.

## 2. Миграции (NFR-M-04)

- Postgres: `uv run payintel migrate --no-clickhouse` или `docker compose run --rm migrate`.
  Откат: `uv run alembic downgrade -1`. Проверка расхождений модели и схемы:
  `uv run alembic check`.
- ClickHouse: `uv run payintel migrate --no-postgres`. Раннер хранит
  применённые версии в `schema_migrations`; файл применяется один раз,
  пустой файл считается ошибкой. Отката нет (в ClickHouse DDL
  необратим): для тестов есть `drop_all` на одноразовой БД.
- Партиции `usage_log` созданы на 15 месяцев вперёд от 2026-10 плюс
  `DEFAULT`; раз в квартал вызывать `payintel`-задачу из этапа 3 или вручную
  `core.partitions.ensure_usage_log_partitions`.

## 3. Справочники и правила (FR-NR-04, FR-DT-01)

1. Правки в `reference/*.yaml` и `rules/**/*.yaml`.
2. `uv run payintel rules-check` — схема, перекрёстные ссылки, регулярные
   выражения, запрет `enabled: true` вместе с `needs_verification: true`.
3. `uv run payintel seed` — upsert с версионированием (`version` + 1 при
   изменении) и записью в `audit_log`. Удалений нет: убранная из YAML запись
   остаётся в БД со статусом, который ей задали (`deprecated`).
4. Изменение паттерна правила без повышения `version` отклоняется
   (`RuleError: bump the version`).

## 4. Gold set и качество (FR-QA-01/02)

```bash
uv run payintel gold import labels.csv --labeled-by <аналитик>
uv run payintel gold import-findings findings.csv     # вывод детектора в store_*/obs_*
uv run payintel eval --report eval-report.json        # exit 1 при precision PSP < 0.95
```

Формат CSV: `domain,entity_type,entity_id,present[,labeled_by,labeled_at,notes]`;
`entity_type ∈ provider|payment_method|platform|country`. Релиз правил
блокируется, если `make eval` в CI красный.

## 5. Feature flags (FR-ADM-05)

`uv run payintel flags list`, `uv run payintel flags set <flag> on|off --actor <кто>`.
Каждое изменение пишется в `audit_log`. Флаги безопасности
(`allow_payment_field_fill`, `allow_account_registration`,
`allow_shipping_step_fill`) и `feature_c2_enabled` меняются только по решению
владельца; `feature_c2_enabled=on` без письменного юридического заключения
запрещён (LR-16).

## 6. Аудит (FR-AB-01, NFR-S-11)

`audit_log` — append-only (триггер запрещает UPDATE/DELETE), каждая строка
содержит `prev_hash` и `row_hash` (SHA-256). Проверка:
`uv run payintel audit verify` (exit 1 при разрыве цепочки). При разрыве:
зафиксировать `first_broken_id`, снять дамп, разбираться как с инцидентом.

## 7. Резервные копии

Postgres: `pg_dump -Fc` ежедневно, хранить 30 дней. ClickHouse: `BACKUP DATABASE
payintel TO S3(...)` еженедельно. MinIO: версионирование бакетов. Проверка
восстановления — раз в квартал на одноразовой БД.

## 8. Открытые юридические вопросы (ТЗ 2.6, ADR-0001, ADR-0008)

До письменного заключения юриста (LR-23) включение регистрации аккаунтов в
магазинах в продакшене и любое использование C1/C2 запрещены. Удаление
аккаунта, созданного сканером, по opt-out — ручная процедура средствами
магазина; заявки фиксируются в `optout_request`.

## 9. Discovery (FR-DS-01..08)

1. Источники: `payintel discovery import <файл> --source tranco|commoncrawl|ct|manual|czds`.
   Форматы: Tranco CSV `rank,domain`; Common Crawl `vertices.txt` (`id<TAB>reversed.host`);
   CT — JSON-строки certstream (`data.leaf_cert.all_domains`); manual — CSV с
   колонкой `domain`; CZDS — зонный файл (NS-записи). Каждый импорт создаёт
   `import_batch` и пишет lineage в `domain_source` (идемпотентно, повтор
   двигает `last_seen`). Невалидные строки считаются и первые 10 печатаются.
2. CZDS выключен флагом `czds_import_enabled` (AS-22); домены только из CZDS
   никогда не попадают в C1 (`discovery.lineage.c1_visible_clause`).
3. Public Suffix List лежит в репозитории (`reference/public_suffix_list.dat`).
   Обновление: скачать `https://publicsuffix.org/list/public_suffix_list.dat`,
   выполнить `payintel discovery update-psl <файл>` (валидирует ≥5000 правил и
   секцию ICANN), закоммитить. Частота — раз в квартал.
4. DNS: `payintel discovery resolve --limit N` опрашивает хосты без проверки
   или старше `scan.no_dns_recheck_days` через Unbound
   (`PAYINTEL_DISCOVERY__DNS_NAMESERVERS`, `__DNS_PORT`). Главный хост без
   A/AAAA → домен `no_dns`; NS/CNAME из `reference/parking_signatures.yaml` →
   `parked`; восстановившийся → `candidate`. Статусы `optout` не трогаются.

## 10. Планировщик и очередь (FR-SC-01..07)

- `payintel scheduler plan [--scan-type light|checkout]` — создаёт/обновляет
  `scan_plan` (приоритет = произведение факторов: ранг Tranco, статус,
  давность, watchlist), удаляет планы opt-out доменов, для checkout исключает
  TLD из `scan.heavy_scan_excluded_tlds` (LR-22).
- Воркер берёт задачи `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED)`
  с арендой `scan.lease_seconds`; `scan_run.id` выводится из (план, аренда),
  поэтому повтор внутри аренды идемпотентен (NFR-R-05). Истёкшая аренда
  перехватывается другим воркером.
- Ошибки: backoff 1/6/24/72 ч, после `max_failures_before_unreachable`
  домен → `unreachable`. Блокировка (401/403/429/503, запрет robots) —
  пауза `scan.blocked_cooldown_days` без увеличения счётчика ошибок.
- `payintel scheduler prioritize <домены…> --actor <кто>` — ручной приоритет
  (FR-SC-07), пишется в `audit_log` как `scan_plan.prioritize`; сбрасывается
  после первого завершённого скана.

## 11. Лёгкий сканер (4.3, FR-LS-01..07)

- Запуск: `payintel worker-light [--once] [--limit N] [--concurrency N] [--worker-id ID]`.
  В compose — сервис `worker-light` профиля `crawl-1`; несколько процессов:
  `docker compose --profile crawl-1 up -d --scale worker-light=4`. Нужны Postgres,
  Redis (politeness общий для всех воркеров), ClickHouse и S3.
- Порядок на хост: `robots.txt` → главная → ≤2 страницы товара → корзина →
  первосторонние скрипты. Страница чекаута не запрашивается. Запрещённое
  robots для `PayIntelBot` не запрашивается; недоступный robots (5xx,
  таймаут) = запрет (консервативно).
- Лимиты: 1 rps на хост и 5 rps на IP (Redis token bucket), 5 МБ на страницу,
  таймауты `light.connect_timeout_seconds`/`read_timeout_seconds`, ≤5 редиректов,
  egress-фильтр на каждом переходе (NFR-S-09, ADR-0010).
- Артефакты: `s3://<bucket>/light/<etld1>/<scan_run_id>/manifest.json`,
  `pages/<тип>.html.gz`, скрипты `js/<aa>/<sha256>.js.gz`. HTML и заголовки
  проходят санитайзер (email/телефоны → маски), cookie/authorization не
  сохраняются. Lifecycle 90 дней задаётся на бакете (LR-08).
- Наблюдения: `obs_scan`, `obs_tech`, `obs_provider`, `obs_payment_method`
  батчами (10 000 строк или 5 с); `store_profile`/`store_provider`
  материализуются в той же транзакции, что `scan_run`.
- Диагностика: `scan_run.stop_reason` (`robots_disallow`, `http_403`,
  `read_timeout`, `egress_blocked: …`), логи JSON с `scan_run_id`.
- Нагрузочный прогон NFR-P-01: `make bench-light` (локальный фикстурный
  сервер, реальные ожидания politeness). Цель ≥20 доменов/с на сервер
  достигается несколькими процессами; один процесс на 4 vCPU даёт ≈15/с.
