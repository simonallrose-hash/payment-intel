# Runbook

Эксплуатационные процедуры для всего, что существует: хранилища, discovery,
сканеры, API/портал, алерты, экспорты, отчёты, compliance.

## 1. Серверы и раскладка (AS-17)

`docker-compose.yml` один, сервисы разнесены профилями:

| Профиль | Сервер | Сервисы |
| --- | --- | --- |
| `app-1` | приложение | Caddy (80/443), Postgres 16, Redis 7, `migrate` (одноразовый), `api` (API + портал + админка), `notifier` (алерты), `exporter` (экспорты, opt-out) |
| `crawl-1` | сканер | Unbound, `worker-light` (масштабируется `--scale worker-light=N`), `worker-checkout` |
| `data-1` | данные | ClickHouse 24.8, MinIO |

Запуск на сервере: `docker compose --profile app-1 up -d` (и аналогично для
остальных). Секреты берутся из `.env`, файл не хранится в репозитории
(NFR-S-06); контейнеры приложения запускаются non-root, read-only FS,
`cap_drop: ALL`.

Сегментация (NFR-S-08). В compose четыре сети: `edge` (только Caddy),
`app` (api, notifier, exporter, audit-verify, Postgres, Redis), `crawl`
(Unbound, `worker-light`, `worker-checkout`) и `data` (ClickHouse, MinIO).
Сервис попадает только в те сети, к которым обращается: воркеры не видят
Redis/Postgres иначе как через `app`, Caddy не видит ничего, кроме `api`.
Между серверами трафик Postgres (5432), Redis (6379), ClickHouse (8123) и
MinIO (9000) идёт только по приватной сети (Hetzner vSwitch или WireGuard);
на публичных интерфейсах файрвол пропускает только 22 (с allow-list IP
администраторов), 80 и 443 на `app-1`. Браузеры Playwright работают в
отдельном контейнере `browser` на `crawl-1` (`playwright run-server`,
только сеть `crawl`: видит Unbound и интернет, не видит Postgres, Redis,
ClickHouse и MinIO; read-only FS, без capabilities, `shm_size 1g`, лимит
памяти 6 ГБ). `worker-checkout` подключается к нему по
`PAYINTEL_CHECKOUT__BROWSER_WS_ENDPOINT=ws://browser:3000/` и сам Chromium
не содержит (образ `app`, лимит 1 ГБ); каждый перезапуск браузера
(`checkout.browser_restart_every_walks`) — новое подключение, то есть
новый браузер на стороне сервера. Дополнительно: egress-фильтр на каждый
переход (NFR-S-09, ADR-0010), отдельный `.env` на `crawl-1` без ключей
шифрования портала. Пустая переменная `BROWSER_WS_ENDPOINT` возвращает
старый режим (Chromium внутри процесса воркера, образ `payintel-checkout`).

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

Ежедневная проверка: сервис `audit-verify` профиля `app-1` раз в сутки
выполняет `payintel audit verify`; при разрыве пишет в stdout строку
`AUDIT CHAIN BROKEN`. Правило алерта (Loki/Promtail или `docker logs`
через cron): любая строка `AUDIT CHAIN BROKEN` → страница дежурному и
заявка на инцидент. Ручная проверка после восстановления из бэкапа
обязательна (п. 7).

## 7. Резервные копии (NFR-R-02…R-04, AC-13)

Цели: RPO ≤ 24 ч для всех данных, RPO 0 для `audit_log` и договоров,
RTO ≤ 8 ч (процедура — п. 18). Хранить 30 дней, копии — в другом ДЦ и у
другого провайдера.

| Что | Как | Когда | Куда |
| --- | --- | --- | --- |
| Postgres (все таблицы) | `pg_dump -Fc payintel` | ежедневно 02:00 UTC | локально 1 день + Storage Box (другой ДЦ, `rclone`/`borg`) + S3-бакет другого провайдера |
| Postgres WAL | `archive_command` → `wal-g wal-push` (или `pgBackRest`) | непрерывно | тот же Storage Box; даёт RPO 0 для `audit_log` и `contract` |
| ClickHouse `payintel` | `BACKUP DATABASE payintel TO S3('…/ch/<дата>')` (инкремент от воскресного полного: `SETTINGS base_backup = …`) | ежедневно 03:00 UTC | бакет другого провайдера |
| MinIO: артефакты | версионирование бакета + `mc mirror --watch` | непрерывно | второй MinIO/S3 у другого провайдера |
| MinIO: экспорты, KYC, договоры, отчёты | `mc mirror` | ежедневно | то же; договоры и KYC-документы — в зашифрованном `age`-архиве |
| `.env`, ключи `age`, TOTP-семя админов | вручную | при каждой смене | password-менеджер компании + бумажный конверт (п. 18) |

Шифрование: дампы и архивы шифруются `age -r <публичный ключ>` до
отправки; бакеты бэкапов — с SSE (`PAYINTEL_S3__SERVER_SIDE_ENCRYPTION=AES256`
для собственного MinIO, SSE-KMS у провайдера). Ротация: 30 суточных копий,
WAL — 30 дней, удаление по lifecycle бакета.

Ежемесячная проверка восстановления (NFR-R-04): в первый понедельник
месяца на одноразовом сервере выполняется п. 18 целиком, время
фиксируется в журнале `ops/restore-drills.md` (дата, кто, длительность,
расхождения); цель — уложиться в 8 ч.

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
   секцию ICANN), закоммитить. Частота — раз в месяц (FR-DS-04); cron на
   `app-1` первого числа: `payintel discovery psl-check --max-age-days 45`
   (exit 1 и строка `STALE` по дате `// VERSION:` в файле → задача на
   обновление).
5. Автоматическая загрузка источников (FR-DS-01): `payintel discovery fetch
   --source tranco|commoncrawl|ct [--out var/sources]` скачивает файл в
   формате, который принимает `import` (Tranco — сегодняшний
   `top-1m.csv.zip`; Common Crawl — `vertices.txt.gz` выбранного индекса,
   `--url` со страницы `https://data.commoncrawl.org/projects/hyperlinkgraph/index.html`;
   CT — RFC 6962 `get-sth`/`get-entries` лога `--log-url`, курсор на лог
   хранится в `--out`, хосты берутся из SAN сертификатов, precert
   пропускаются, `--max-entries` на запуск). Затем `payintel discovery
   import <файл> --source …`. Cron на `app-1`: Tranco — ежедневно, CT —
   каждый час (инкрементально по курсору), Common Crawl — при выходе
   нового индекса (раз в 1–2 месяца).
6. Проверка AC-01 (≥ 1 млн e-commerce доменов из ≥ 2 источников):
   ```sql
   SELECT count(*) FROM domain d
    WHERE d.status = 'ecommerce'
      AND (SELECT count(DISTINCT s.source) FROM domain_source s
            WHERE s.domain_id = d.id AND s.source <> 'czds') >= 2;
   ```
   Отдельно `GROUP BY country` по `store_profile.country` даёт разбивку по
   странам для приёмки.
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
  сохраняются. Lifecycle `retention.raw_artifacts_days` (90) на префиксы
  `light/` и `checkout/` воркер ставит на бакет сам при старте
  (`ObjectStore.ensure_lifecycle`, LR-08); проверка — `mc ilm ls`.
- Наблюдения: `obs_scan`, `obs_tech`, `obs_provider`, `obs_payment_method`
  батчами (10 000 строк или 5 с); `store_profile`/`store_provider`
  материализуются в той же транзакции, что `scan_run`.
- Диагностика: `scan_run.stop_reason` (`robots_disallow`, `http_403`,
  `read_timeout`, `egress_blocked: …`), логи JSON с `scan_run_id`.
- Нагрузочный прогон NFR-P-01: `make bench-light` (локальный фикстурный
  сервер, реальные ожидания politeness; `BENCH_PROCESSES=4` — по процессу
  на CPU, как `--scale worker-light=N` в compose). Результаты и расчёт
  серверов — `docs/capacity.md`.

## 12. Сканер чекаута (4.4, FR-CW-01..14)

- Запуск: `payintel worker-checkout [--once] [--limit N] [--concurrency N] [--worker-id ID]`.
  В compose — сервис `worker-checkout` профиля `crawl-1` плюс контейнер
  `browser` (`playwright run-server`, образ цели `checkout` в
  `docker/app.Dockerfile`; NFR-S-08, §1). Нужны Postgres, Redis,
  ClickHouse, S3 и `PAYINTEL_SECRETS__ENCRYPTION_KEY` (без ключа регистрация
  аккаунтов отключена — FR-CW-12). Один браузер на процесс, перезапуск
  каждые `checkout.browser_restart_every_walks` (50) проходов, по умолчанию
  `checkout.concurrency_per_worker` (8) контекстов параллельно, не более
  одного прохода на eTLD+1 одновременно (FR-SC-06).
- Порядок на хост: `robots.txt` (до 512 КБ; запрет главной — стоп
  `navigation/robots_disallowed` без запуска браузера) → главная (выбор
  адаптера по профилю или правилам платформ) → товар (≤3 кандидата) →
  корзина → чекаут → стена входа (гость → аккаунт → регистрация по флагу) →
  адрес/доставка (≤6 шагов) → шаг оплаты. Дедлайн 90 с, память 1 ГБ JS-heap,
  ≤40 кликов. Кнопка оплаты/заказа не нажимается никогда (ADR-0013).
- Флаги читаются при лизинге задачи: `allow_shipping_step_fill`,
  `allow_account_registration`, `allow_payment_field_fill`
  (`payintel flags set …`, FR-ADM-05).
- Гео-параметр (FR-CW-11, ADR-0026): проход идёт с языком и адресом страны
  магазина. Если страна магазина отличается от
  `PAYINTEL_CHECKOUT__EGRESS_COUNTRY` (DE) и в
  `PAYINTEL_CHECKOUT__GEO_PROXIES` (JSON `{"FR": "http://user:pass@host:port"}`,
  схемы http/https/socks5) есть прокси этой страны, контекст браузера
  идёт через него — только для геолокации, решение принимается до прохода и
  пишется в манифест (`geo.reason`, хост прокси без учётных данных).
  Прокси никогда не включается в ответ на блокировку (LR-03).
- Адаптеры (FR-CW-03): woocommerce, magento2, shopware6, prestashop,
  shopify (ADR-0029), opencart, oxid, shopware5, magento1, bigcommerce
  (ADR-0030); остальные платформы (в т. ч. plentymarkets, JTL) — эвристика.
  Лимиты по ASN (п. 21) действуют и на проходы чекаута.
- Хостед-чекауты (ADR-0032): проход не покидает eTLD+1 магазина. Ссылка из
  корзины на хост из `reference/hosted_checkouts.yaml` или редирект на него
  → стоп `checkout/hosted_checkout_external` (`stop_detail` = `<provider>:
  <host> (link|redirect)`), провайдер пишется в `obs_provider`
  (`active_on_checkout`, `high`), хост — в `obs_checkout_host`. Редирект на
  любой другой чужой хост → `navigation/navigation_error` («left <etld1>»).
  Новый хост добавляется в справочник только со ссылкой на официальную
  страницу провайдера, иначе остаётся `needs_verification` и не матчится.
- Артефакты: `s3://<bucket>/checkout/<etld1>/<scan_run_id>/manifest.json`
  (журнал действий, шаги, запросы, находки, стоп), `payment_step.html.gz`,
  `payment_block.html.gz`, `screenshot.jpg` (≤300 КБ), `har.json.gz` (без
  cookie, authorization и тел), при стопе `stop.jpg` + `stop.html.gz`.
  Lifecycle 90 дней на бакете ставит воркер при старте (п. 11, LR-08).
- Наблюдения: `obs_scan` (`scan_type=checkout`), `obs_scan_stop`,
  `obs_provider` (с `active_on_checkout`), `obs_payment_method`, `obs_tech`,
  `obs_checkout_host`; состояние: `scan_run`, `store_profile`
  (`checkout_status`, `coverage`, `checkout_country`, `acquirer_hidden`,
  `last_checkout_scan_at`), `store_provider`/`store_payment_method`/
  `store_checkout_host` по правилу «два подряд», `change_event` (ADR-0015),
  `store_account` (пароль AES-GCM).
- Расписание: успех → через `scan.checkout_interval_days` (30; в watchlist 7);
  `blocked` → cool-down `scan.blocked_cooldown_days` (30) с `last_error`;
  `timeout`/`error` → backoff FR-SC-05.
- Диагностика: `scan_run.stop_step`/`stop_reason`, `plan.last_error`, логи
  JSON с `scan_run_id`; выборка стопов со скриншотами —
  `payintel quality stop-sample <step> <reason> [--platform …] [--csv file]`.
- Повторный проход с трейсом (FR-QA-06, ADR-0031): в админке на странице
  домена форма «Re-run checkout walk» (чекбокс трейса включён по умолчанию)
  или `payintel scheduler prioritize <host> --scan-type checkout --trace`.
  План получает `trace_requested`/`requested_by` и `next_scan_at = now`;
  воркер пишет `trace.zip` рядом с манифестом (`scan_run.trace_key`) и
  сбрасывает флаг. Скачать: ссылка `trace` в таблице прогонов
  (`GET /admin/domains/{host}/runs/{run_id}/trace`, роль `staff_analyst`);
  смотреть: `uv run playwright show-trace trace.zip`. Для opt-out и доменов
  вне тяжёлого скана перезапуск не ставится.
- Проверка guardrails (AC-04): `PAYINTEL_TEST_TRAP_RUNS=1000 uv run pytest
  tests/e2e/test_checkout_walk.py -k trap_shop` (≈15 мин на 4 vCPU;
  в CI — еженедельный job `trap-1000`, в `make test` — 100 прогонов).

## 13. Еженедельный разбор остановок (FR-QA-06)

1. `payintel quality stops --days 7` — распределение по причинам, шагам,
   платформам, сравнение релизов сканера; `payintel quality stop-alerts`
   пишет алерты (+5 п. п. за неделю или после релиза; `other` > 5 %) в
   `quality_alert` для `staff_analyst`. `payintel quality dashboard` —
   доля проходов до чекаута/оплаты по платформам, `blocked`, уверенность,
   свежесть, события в сутки с всплесками (FR-QA-03).
2. Для каждой из топ-5 причин: `payintel quality stop-sample <step> <reason>
   --platform <id> --csv stops.csv` → до 20 доменов со скриншотом
   (`screenshot_key`) и DOM (`dom_key`) в S3. Если скриншота и DOM мало —
   повторный проход с трейсом из админки (п. 12, ADR-0031) и
   `playwright show-trace`.
3. По каждой причине заводится задача: адаптер (селекторы), эвристика или
   словарь (`reference/checkout_dictionary.yaml`: `step_actions`,
   `guest_words` …). Никогда — ослабление политики кликов (ADR-0013).
4. Если `other` > 5 % — таксономия `reference/stop_reasons.yaml` расширяется
   новым кодом с миграцией словаря; старые строки остаются `other` с
   пояснением в `stop_detail`.
5. DOM-снимок реального стопа превращается в фикстуру
   `tests/fixtures/shops/<имя>/` (конфиг `shop.json` симулятора или
   статический HTML) и регрессионный тест в `tests/e2e/test_checkout_walk.py`.
6. После релиза сканера (`scanner_version`) или правил сравнение «до/после»
   смотрится в `payintel quality stops` (блок `release …`) через 2–3 дня.

## 14. API, портал и админка (FR-API-*, FR-UI-*, FR-ADM-*)

- Сервис `api` (`payintel api serve`, uvicorn, 2 воркера) обслуживает
  `/v1/*`, `/portal/*`, `/admin/*`, публичные `/bot`, `/optout`, `/dsar`,
  `/healthz`, `/openapi.json`. Caddy проксирует всё на `api:8000` и ставит
  `X-Forwarded-For` — по нему считаются IP-списки и аудит.
- Обязательные секреты: `PAYINTEL_SECRETS__ENCRYPTION_KEY` (TOTP, секреты
  webhook), `PAYINTEL_SECRETS__API_KEY_PEPPER`; при их смене старые ключи и
  TOTP перестают работать — ротация только с перевыпуском.
- Первый staff-пользователь: `payintel users create --email … --staff
  --role staff_admin` (пароль запрашивается, TOTP включается при первом
  входе). Остальных создаёт `staff_admin` в `/admin/users`.
- Организация: `/admin/orgs` → создать → досье KYC (цель, бенефициары с
  долями, документы в S3, санкции, видеозвонок) → решение `approved` →
  статус `approved` → контракт (номер, продукт, срок, цели, файл) →
  entitlement (профиль, страны/платформы, лимиты, IP) → статус `active`.
  Без одобренного досье переход в `approved`/`active` невозможен (409).
  То же из CLI: `payintel orgs create`, `payintel keys issue`.
- Ключи API выдаёт `org_admin` в `/portal/keys` (или `payintel keys issue
  --org <id>`); ключ виден один раз. Отзыв — там же, действует сразу.
- Блокировка входа: 10 неудач → 15 минут; `staff_admin` может сбросить
  пароль в `/admin/users`. Сессии — 12 ч без активности, выход отзывает.
- Диагностика: каждый ответ несёт `X-Request-Id`; логи structlog с ним же;
  `usage_log` — что и сколько отдал клиенту; `audit_log` — все действия
  staff и клиентов в портале (`/admin/audit?verify=1` проверяет хэш-цепочку,
  `payintel audit verify` — то же из CLI).
- Правила детекции: `/admin/rules` — список текущих версий, правка создаёт
  новую версию, предпросмотр на gold set до сохранения, выключение —
  версия с `enabled=false`; воркеры подхватывают при перезапуске
  (`docker compose restart worker-light worker-checkout`). ADR-0020.
- Feature flags: `/admin/flags` (только `staff_admin`), `feature_c2_enabled`
  остаётся выключенным.
- Ревью находок (FR-QA-05, ADR-0022): `/admin/review` — очередь провайдеров
  и способов оплаты по возрастанию уверенности, улики из ClickHouse со
  ссылками на скриншоты; «подтвердить»/«отклонить» пишет gold-метку,
  отклонённая находка скрывается из API, экспортов и отчётов
  (`suppressed`) до повторного подтверждения.
- Повторная детекция (FR-DT-12): `payintel redetect [--host <домен>]
  [--limit N] [--apply] [--csv findings.csv]` — пересчёт по сохранённым
  артефактам текущим набором правил; без `--apply` только отчёт о
  добавленном/исчезнувшем.
- Доля рынка (`GET /v1/stats/market-share`) отдаётся из снимка
  `market_share_cell` (ADR-0033, NFR-P-05). Cron на `app-1` раз в час:
  `payintel stats refresh` (на 1 млн магазинов ≈ 1,5 мин; `as_of` в ответе
  = время снимка). Без снимка API считает живым запросом — на большой базе
  это десятки секунд, поэтому после первой миграции `refresh` обязателен.
- Метрики Prometheus (NFR-P-07): `GET /metrics` отвечает только с
  `Authorization: Bearer $PAYINTEL_SECRETS__METRICS_TOKEN` (без токена —
  404, endpoint выключен); scrape-config Prometheus — `bearer_token`.
  Гистограмма `payintel_api_request_seconds{route,method,status}` — по
  шаблону маршрута, из неё считаются p95/p99 NFR-P-03..05. Notifier
  экспортирует свои метрики на `payintel alerts dispatch --metrics-port
  9108` (только 127.0.0.1; Prometheus ходит через сеть compose). Воркеры —
  `payintel worker-light --metrics-port 9109` и `worker-checkout
  --metrics-port 9110`: `payintel_light_scans_total{status}`,
  `payintel_checkout_walks_total{status}`, `payintel_checkout_walk_seconds`
  (NFR-C-02, стоимость прохода — `docs/capacity.md` §8).
- Загрузка серверов (FR-ADM-03): `/admin/crawler` показывает очереди,
  пропускную способность, ошибки и таблицу «Servers and workers»: каждый
  воркер (`worker-light`, `worker-checkout`) раз в
  `PAYINTEL_SCAN__HEARTBEAT_SECONDS` (30) пишет строку `worker_heartbeat`
  — хост, PID, load average 1/5 мин, число CPU, RAM занято/всего
  (`/proc/meminfo` хоста, так как cgroup контейнера их не скрывает),
  задач в полёте и выполнено; строка краснеет как `stale` без
  heartbeat более 3 интервалов (воркер завис или упал), `stopped` после
  штатной остановки. Load выше числа CPU или RAM > 90 % — хост
  насыщен (docs/capacity.md). Диск и сетевые метрики хостов — из
  `node_exporter` (порт 9100 только в приватной сети) в Grafana;
  алерты — по Prometheus, а не по таблице.
- Пороги качества (NFR-Q-01…09): `payintel quality thresholds` печатает
  текущие значения и нарушения, `/admin/quality` показывает их красным;
  `payintel quality event-sample --days 7 --csv sample.csv` даёт выборку
  200 событий на ревью, `quality event-review <id> accepted|rejected`
  фиксирует решение, доля отклонённых считается в дашборде (NFR-Q-09).
- Анти-абьюз, re-KYC, лимиты по ASN — пп. 19–21.

## 15. Алерты (FR-AL-*)

- Сервис `notifier` = `payintel alerts dispatch` каждые 30 с: матчинг новых
  `change_event` по правилам → `alert_delivery` → отправка (webhook с
  подписью, Telegram). Ретраи 1/3/7/13 ч, после 5 неудач `failed`.
- Неисправный webhook клиента виден в `/portal/alerts` (последняя ошибка,
  попытки); перевыпуск секрета — удалить и создать webhook заново.
- Дайджесты уходят в `alerts.digest_hour_utc` (07:00 UTC); вручную —
  `payintel alerts dispatch --once --force-digests`.
- Задержка скан → событие → доставка (NFR-P-07, ≤ 6 ч): гистограмма
  `payintel_alert_latency_seconds{channel}` (от `scan_run.finished_at` до
  `delivered_at`) и gauge `payintel_alert_backlog_age_seconds` — возраст
  самого старого события с недоставленным алертом. Правило алертинга:
  `payintel_alert_backlog_age_seconds > 14400` (4 ч) — предупреждение,
  `> 21600` — нарушение порога. Счётчик
  `payintel_alert_deliveries_total{channel,outcome}` показывает ретраи.
- Telegram: токен бота в `PAYINTEL_SECRETS__TELEGRAM_BOT_TOKEN`; клиент
  указывает chat id в правиле. Egress только на `api.telegram.org` и URL
  webhook клиента (приватные адреса запрещены).
- Сегментные правила (FR-AL-05, ADR-0021): правило без watchlist действует
  на весь сегмент entitlement, при желании суженный странами/платформами
  (вне гранта — 422).
- Всплеск `provider_removed` (FR-QA-04): notifier перед каждой
  диспетчеризацией (и `payintel quality anomalies`) сравнивает последние
  24 ч с средним за 7 дней; при > 3× поднимается quality-алерт
  `provider_removed_spike`, доставки по провайдеру удерживаются. Аналитик
  в `/admin/quality` подтверждает (доставки уходят) или отклоняет (доставки
  → `failed` с заметкой). Без решения доставки висят — проверяйте список
  удержаний при каждом разборе.

## 16. Экспорты и отчёты (FR-EX-*, FR-RP-*)

- Сервис `exporter` = `payintel exports run` раз в 60 с: ставит
  периодические экспорты по `export_schedule`, строит `pending` задания в
  S3 (`exports/<org>/<job>.<fmt>`), ссылки на 72 ч. Полные снимки ждут
  одобрения в `/admin/exports` (`staff_compliance`).
- Утечка файла: по метаданным или порядку строк →
  `payintel exports identify <файл>` не входит в этап 3; используйте
  `exports.watermark.order_matches` из Python-сессии с `watermark_id`
  кандидатов из `export_job`. Канареечные домены — таблица `canary`:
  DNS/HTTP-обращение к `c-*.<canary zone>` указывает на экспорт.
- Отчёты C (XLSX + CSV zip + PDF, лист/раздел «Методология»):
  `/admin/reports` (`staff_analyst`) или `payintel report build --country DE
  --out ./out`. Ячейки < 30 магазинов скрываются, редкие провайдеры →
  `other`; сводка `min_published` в задании должна быть ≥ 30. Клиент
  получает отчёт (включая PDF) в `/portal/reports`, если задание привязано
  к его организации. PDF строится fpdf2 с вшитыми шрифтами DejaVu
  (ADR-0027), системные библиотеки не нужны.
- Публичные сводки (FR-RP-05, ADR-0028): у готового отчёта в
  `/admin/reports` кнопка «Publish summary» → анонимные страницы
  `/reports` и `/reports/<slug>` (+ `.pdf`) с топ-5 провайдеров/методов на
  ячейку, без доменов. «withdraw» делает страницы 404. Обе операции в
  аудите (`report.publish`/`report.unpublish`).
- Квартальные отчёты об использовании (FR-AB-05): `payintel abuse
  usage-report --year 2026 --quarter 3 [--org <id>]` (по умолчанию —
  прошлый квартал, все организации) или кнопка «Build quarterly report»
  на карточке организации → `usage/<org>/<period>.xlsx` в бакете
  экспортов; клиент видит их в `/portal/usage`.

## 17. Opt-out и DSAR (FR-OO-*, FR-DS-09, LR-06)

- Владелец домена подаёт заявку на `/optout`, получает токен и публикует
  `payintel-optout=<token>` как TXT у apex или в
  `https://<домен>/.well-known/payintel-optout.txt`.
- Проверку доказательств раз в час выполняет цикл `payintel exports run`
  (`--optout-interval`, по умолчанию 3600 с); вручную — `payintel optout
  verify-pending`. При успехе домен сразу исчезает из API/экспортов
  (lineage guard) и `apply` снимает планы сканирования (в пределах 72 ч,
  FR-OO-02). Ручное подтверждение (письмо): `/admin/optout`.
- Аккаунты, созданные сканером в магазине владельца (FR-CW-12): при
  применении opt-out `AccountManager.delete` переводит `store_account` в
  `deleted`, стирает шифротекст пароля и пишет `store_account.delete` в
  аудит; сканер больше не входит в магазин. Удаление самого аккаунта на
  стороне магазина (кнопка «удалить аккаунт» или письмо в поддержку
  магазина) — ручной шаг `staff_compliance` в течение 72 ч, фиксируется в
  `optout_request.note`. Вручную: `payintel accounts list`,
  `payintel accounts delete <hostname>`.
- DSAR (`/dsar`) создаёт заявку; `staff_compliance` берёт и закрывает её в
  `/admin/dsar` в срок 30 дней; `payintel dsar list` для контроля.

## 18. Восстановление на чистом сервере (NFR-R-03, AC-13) и ключи

Норматив — 8 ч от решения о восстановлении до рабочего API. Шаги (время —
по drill на стенде 1 млн магазинов; на целевых серверах быстрее):

1. (15 мин) Поднять ОС, Docker, приватную сеть; склонировать репозиторий
   нужного тега; восстановить `.env` и ключи `age` из password-менеджера.
2. (10 мин) `docker compose --profile app-1 up -d postgres redis`,
   `--profile data-1 up -d clickhouse minio`.
3. (1–2 ч на 10 ГБ) Postgres: `pg_restore -d payintel --no-owner <дамп>`,
   затем WAL до нужной точки (`wal-g backup-fetch` + `recovery_target_time`),
   если нужен RPO 0 для `audit_log`.
4. (30 мин) `payintel audit verify` — цепочка должна быть целой до
   `first_broken_id = NULL`; разрыв = восстанавливать более ранний WAL.
5. (1–3 ч на 1 ТБ) ClickHouse: `RESTORE DATABASE payintel FROM S3('…')`.
6. (параллельно) MinIO: `mc mirror` из зеркала обратно; проверить
   `mc ilm ls` (lifecycle пересоздаст воркер при старте).
7. (5 мин) `docker compose run --rm migrate` (ничего не должно
   примениться), `payintel stats refresh`.
8. (10 мин) `up -d` остальных сервисов; проверки: `/healthz`,
   `GET /v1/stores/{domain}` тестовым ключом, `/admin/quality`, одна
   выгрузка `payintel exports run --once`.
9. Записать drill в `ops/restore-drills.md`.

Ключи: `PAYINTEL_SECRETS__ENCRYPTION_KEY` и `API_KEY_PEPPER` без копии
невосстановимы — TOTP и ключи клиентов придётся перевыпустить.

В дампы входят (п. 7) таблицы `portal_session`, `api_key`, `export_job`,
`canary`, `audit_log` (цепочка хэшей проверяется после восстановления:
`payintel audit verify`). Файлы экспортов и отчётов — в бакете
`PAYINTEL_S3__BUCKET_EXPORTS`, KYC-документы и контракты — там же под
`kyc/` и `contracts/`.
К этапу 4 добавляются `abuse_incident`, `canary_hit`, `usage_report`,
`finding_review`, `asn_limit`, поля re-KYC на `kyc_dossier`,
`report_job.public_summary`; PDF отчётов и публичные сводки — в бакете
экспортов под `reports/`. Таблица ip2asn (п. 21) не бэкапится: она
скачивается заново.

## 19. Анти-абьюз (FR-AB-02…05, ADR-0023)

- `payintel abuse detect [--once] [--interval 900]` — отдельный процесс
  (цикл раз в 15 мин; в compose добавьте сервис рядом с `exporter`),
  прогоняет детекторы по `usage_log` за 24 ч: доля отказов `outside_segment`,
  всплеск записей (> 3× среднего за 14 окон), перебор каталога, новая сеть
  /16, зондирование полей. Пороги — группа `PAYINTEL_ABUSE__*`
  (`settings.py`).
- Критичная находка (записи > 10× или перебор ≥ 3000 доменов) **сразу**
  ограничивает организацию: все её ключи получают 403
  `org_restricted`. Инциденты — `/admin/abuse` (`staff_compliance`);
  «resolve» с галочкой «lift restriction» снимает ограничение, «dismiss» закрывает без него.
  Отключить автоограничение: `PAYINTEL_ABUSE__AUTO_RESTRICT=false`.
- Канарейки (FR-AB-04): обращения к доменам под
  `PAYINTEL_IDENTITY__CANARY_ZONE` подаются из логов резолвера, веб-хоста и
  почты: `payintel abuse canary-hits <файл> --kind dns|http|email` (строки
  `<ts> <домен-или-адрес> [source] [detail]`). Попадание связывается с
  экспортом и открывает `high`-инцидент.
- `payintel abuse incidents` — открытые инциденты; квартальные отчёты —
  п. 16.

## 20. Re-KYC и санкционный скрининг (FR-KYC-03/06, ADR-0024)

- Одобрение досье ставит срок пересмотра через 365 дней
  (`PAYINTEL_COMPLIANCE__REKYC_INTERVAL_DAYS`); изменение бенефициаров
  делает пересмотр должным сразу. `payintel orgs rekyc [--within-days 30]
  [--remind]` показывает, что истекает, и с `--remind` шлёт по одному
  напоминанию на срок (аудит `kyc.review_reminder`, Telegram в
  `PAYINTEL_COMPLIANCE__STAFF_TELEGRAM_CHAT_ID`, если задан). Запускать
  ежедневно из планировщика оператора.
- Обзор админки показывает `rekyc_due` / `rekyc_overdue`; на карточке
  организации «Open re-KYC» снимает решение и скрининг (досье остаётся),
  далее — обычный цикл решения. Просрочка не блокирует организацию
  автоматически — это решение комплаенса.
- Скрининг: `PAYINTEL_SECRETS__OPENSANCTIONS_API_KEY` (коммерческий ключ
  OpenSanctions); «Screen via OpenSanctions» на карточке или
  `payintel orgs screen <id>`
  запрашивает `POST /match/default` для организации и бенефициаров
  (порог 0.7, cutoff 0.5, параметры в `PAYINTEL_COMPLIANCE__SANCTIONS_*`).
  Результат `match` блокирует одобрение; `potential_match` требует
  ручного решения; недоступность сервиса — 503, «clear» при ошибке не
  ставится. Egress: `api.opensanctions.org`.

## 21. Жалобы хостеров и лимиты по ASN (FR-OO-04, ADR-0025)

- Таблица ip2asn: скачать `https://iptoasn.com/data/ip2asn-combined.tsv.gz`
  (PDDL) на хост воркеров и указать `PAYINTEL_SCAN__ASN_TABLE_PATH`;
  обновлять еженедельно (cron). Без таблицы жалобы принимаются только по
  номеру ASN, а воркеры не применяют лимиты по ASN (предупреждение в логе
  при старте).
- Жалоба: `payintel crawler complaint AS12345 --source abuse@hoster
  --note "ticket 4711"` или по IP (`203.0.113.7`, нужна таблица); в
  админке — `/admin/crawler`, форма «Apply limit». Действует с ближайшего
  обновления политики на воркерах (≤ 60 с): per-host и per-IP скорости ×
  `scan.complaint_rps_factor` (0.1) и общий лимит `scan.complaint_asn_rps`
  (1 запрос/с) на всю сеть. Повторная жалоба делит множитель пополам.
- Пакетно: `payintel crawler complaints <файл>` (строки
  `TARGET<TAB>SOURCE[<TAB>NOTE]`). Список: `payintel crawler asn-limits`.
  Снятие после разбора — только `staff_admin`: `payintel crawler lift
  AS12345 --note "…"` или кнопка «Lift» (аудит `asn_limit.lift`).

## 22. Пересмотр доступов и ответственный за злоупотребления (LR-18, FR-AB-01)

- Ответственный за злоупотребления (abuse officer) — роль `staff_compliance`
  с явным назначением в `ops/roles.md` (имя, замена на время отпуска).
  Он разбирает `abuse_incident`, обращения на `/bot` и жалобы хостеров
  (п. 21), решает о `suspend`/`restrict` организации и ведёт переписку
  с клиентом; каждое решение — запись в `audit_log` через админку.
- Квартальный пересмотр доступов (первая неделя квартала):
  1. `payintel users list` (роль, организация, последний вход, состояние)
     → для каждого пользователя подтвердить роль и необходимость;
     лишних — `payintel users disable --email …` (блокирует вход навсегда
     и отзывает сессии портала, запись `user.disable` в аудите).
  2. `/admin/orgs` → для каждой организации: договор не истёк, entitlement
     соответствует договору, ключи API (`/portal/keys` глазами staff) —
     неиспользуемые > 90 дней (`api_key.last_used_at`) отзываются.
  3. Доступ к серверам: список SSH-ключей на `app-1/crawl-1/data-1`
     сверяется со списком сотрудников; лишние удаляются.
  4. Результат — запись в `ops/access-reviews.md` (дата, кто проверял,
     что изменено) и строка `audit_log` `access_review.completed`.
