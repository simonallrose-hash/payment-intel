# Runbook (этап 0)

Эксплуатационные процедуры для того, что уже существует. Разделы для
сканера, API и алертов добавляются на этапах 1–3.

## 1. Серверы и раскладка (AS-17)

`docker-compose.yml` один, сервисы разнесены профилями:

| Профиль | Сервер | Сервисы |
| --- | --- | --- |
| `app-1` | приложение | Caddy (80/443), Postgres 16, Redis 7, `migrate` (одноразовый), далее api/portal (этап 3) |
| `crawl-1` | сканер | Unbound, воркеры light/checkout (этапы 1–2) |
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
