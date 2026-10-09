# API /v1 (этап 3)

Машинный контракт — `GET /openapi.json` (OpenAPI 3.1, без Swagger UI; портал
рендерит его на странице «Документация»). Этот документ — краткое описание
для клиента. Решения по спорным местам: [ADR-0016](adr/0016-api-entitlements-and-suppression.md).

## Транспорт и аутентификация

- HTTPS за Caddy (NFR-S-01), JSON. Все ответы несут `X-Request-Id`,
  `Cache-Control: no-store`, CSP и `X-Content-Type-Options: nosniff`.
- `Authorization: Bearer pik_xxxxxxxx.<секрет>` (FR-API-02). Ключ выдаётся в
  портале `org_admin`-ом и показывается один раз; в БД хранится только
  HMAC-SHA256 с pepper. Ключ несёт scopes (`stores:read`, `changes:read`,
  `stats:read`, `watchlists:write`, `webhooks:write`, `exports:write`,
  `usage:read`), срок действия и список IP (опционально, через entitlement).
- Каждый запрос журналируется (`usage_log`: организация, ключ, endpoint,
  параметры, число записей, IP, длительность, request id) — FR-API-09.

## Entitlements и ошибки

На каждом запросе (FR-API-06) проверяются: статус организации `active`,
контракт в сроке, активный entitlement, IP-список, scope ключа, сегмент
(страны/платформы), профиль полей, rate limit и квоты записей. Отказ —
`403 application/problem+json`:

```json
{"type":"about:blank","title":"Forbidden","status":403,
 "code":"forbidden","reason":"outside_segment",
 "detail":"store is outside the segment","instance":"/v1/stores/x.fr"}
```

`reason`: `org_not_active`, `contract_not_in_term`, `no_entitlement`,
`ip_not_allowed`, `scope_missing`, `role_forbidden`, `outside_segment`,
`field_profile_insufficient`, `c2_disabled`, `key_revoked`, `key_expired`,
`watchlist_limit_exceeded`, `export_limit_exceeded`.

Лимиты (FR-API-07): `429` с `Retry-After`, `code = rate_limited` (окно 1 с,
по умолчанию 10 rps) или `quota_exceeded` (по умолчанию 50 000 записей в
сутки, 1 000 000 в месяц; экспорт считается в ту же квоту). Прочие коды:
`400 validation_error` (с `errors[]`), `404 not_found`, `409 conflict`.

Домены только из CZDS, домены с opt-out и не e-commerce отсутствуют во всех
ответах (FR-DS-09, FR-OO-02): для клиента их нет.

## Профили полей (AS-23, AC-07)

| Профиль | Схема | Что добавляет |
|---|---|---|
| `c1_basic` | `StoreBasic` | domain, as_of, confidence, coverage, platform, country, checkout, providers (id, name, role, confidence), payment_methods, methodology_url |
| `c1_full` | `StoreFull` | + currency, vertical, checkout_psp_hosts (хосты PSP на шаге оплаты), evidence (тип сигнала, страница), recent_changes, traffic_rank, даты сканов |
| `c2_risk` | — | недоступен, пока `feature_c2_enabled=false` |

Поля C1 (сторонние хосты не-PSP, плагины, версии платформ/плагинов,
внутренние скоры) отсутствуют в схемах физически и не могут быть выданы ни
API, ни экспортом.

## Операции

| Метод и путь | Scope | Назначение |
|---|---|---|
| `GET /v1/stores` | stores:read | Поиск: `country`, `platform`, `provider`, `without_provider`, `provider_role`, `payment_method`, `providers_min/max`, `min_confidence`, `first_seen_from/to`, `last_seen_from/to`, `changed_from/to`, `domain_prefix`, `sort=domain|traffic_rank|last_seen`, `cursor`, `limit≤1000` (FR-API-03/05) |
| `GET /v1/stores/{domain}` | stores:read | Карточка магазина по профилю (FR-API-04) |
| `GET /v1/stores/{domain}/history` | stores:read | История изменений магазина (FR-HI-05) |
| `GET /v1/changes` | changes:read | Лента изменений: `since`, `until`, `event_type`, `country`, `platform`, курсор |
| `GET /v1/stats/market-share` | stats:read | Доли PSP по ячейкам страна×платформа с CI Уилсона; ячейки < `min_cell_size` (30) скрыты, редкие провайдеры → `other` (LR-19) |
| `GET /v1/providers`, `GET /v1/payment-methods` | stores:read | Справочники |
| `GET/POST /v1/watchlists`, `GET/DELETE /v1/watchlists/{id}`, `GET/POST /v1/watchlists/{id}/domains`, `DELETE …/domains/{domain}` | watchlists:write (чтение — stores:read) | Списки наблюдения, лимит из entitlement (FR-AL-01) |
| `GET/POST /v1/webhooks`, `GET/PATCH/DELETE /v1/webhooks/{id}` | webhooks:write | Webhook-получатели; секрет `whsec_…` только в ответе на создание (FR-AL-04) |
| `POST /v1/exports` → 202, `GET /v1/exports`, `GET /v1/exports/{id}` | exports:write / stores:read | Экспорты (FR-EX-01…06) |
| `GET /v1/usage` | usage:read | Использование квот за день/месяц |

Пагинация: `{"items": [...], "next_cursor": "…"}`; курсор непрозрачен и
привязан к `sort`.

## Webhook (FR-AL-04)

`POST <url>` с телом `{"event_id", "domain", "type", "entity_type", "entity",
"old_value", "new_value", "detected_at", "country", "platform_id"}` (или
`{"digest": [...]}` для дайджестов) и заголовком
`X-PayIntel-Signature: t=<unix>,v1=<hex>`, где
`v1 = HMAC-SHA256(secret, "<t>." + body)`. Проверяйте `|now − t| ≤ 300 с`.
Ответ 2xx — доставлено; иначе повторы через 1, 3, 7, 13 ч (5 попыток, ≤ 24 ч).

## Экспорты (FR-EX-01…06, ADR-0018)

- `type`: `full_snapshot` (требует одобрения `staff_compliance`), `increment`
  (обязателен `since`), `market_aggregates`; `format`: `parquet` | `csv`.
- Статусы: `awaiting_approval → pending → building → done | failed`;
  `download_url` — подписанная ссылка на 72 ч, повторный `GET` выдаёт новую.
- Содержимое соответствует профилю полей; строки упорядочены водяным знаком;
  3–10 канареечных записей; метаданные (`payintel.export_id`,
  `payintel.watermark`, `payintel.org_id`, `payintel.methodology`) — в
  Parquet key-value metadata, в CSV — первая строка-комментарий `# …`
  (читайте с `comment="#"`), в агрегатах — поле `_meta`.
- Лимит строк — `export_max_rows` entitlement (403 `export_limit_exceeded`);
  периодические экспорты по `export_schedule` создаёт сервис `exporter`.

## Методология

Каждый ответ содержит `methodology_url` (FR-API-10). Описание уверенности,
покрытия и подавления — [methodology.md](methodology.md).
