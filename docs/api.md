# API (контракт, реализация на этапе 3)

Этап 0 фиксирует только решения, влияющие на схему данных.

- Транспорт: HTTPS за Caddy, JSON, пагинация курсором, `page_size ≤ 1000`
  (FR-API-05), лимиты по умолчанию 10 rps и 50 000 записей в сутки на
  организацию (FR-API-07), `usage_log` партиционирован по месяцам.
- Аутентификация: API-ключ в заголовке `Authorization: Bearer`. В БД хранится
  только HMAC-SHA256 с pepper (`core/crypto.hash_api_key`), префикс для
  поиска (NFR-S-02).
- Авторизация: entitlements проверяются на сервере на каждом запросе
  (`entitlement.field_profile`, лимиты, список стран/вертикалей). Профиль
  `c2_risk` недоступен, пока `feature_c2_enabled=false`.
- Ошибки: RFC 9457 `application/problem+json` с машинным `code` из
  `core/errors.py` (`validation_error`, `not_found`, `forbidden`,
  `c2_disabled`, …).
- Поля ответа: отдельные Pydantic-схемы на профиль (`basic`, `extended`,
  `c2_risk`); поля C1 отсутствуют в схемах `basic`/`extended` физически,
  а не фильтруются (AS-23, AC-07).
- Экспорт: асинхронные задания `export_job`, форматы CSV/JSONL/Parquet,
  ссылки на 72 часа (FR-EX-04), канареечные записи (`canary`) на каждую
  организацию.
