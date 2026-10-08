# Методология (этап 0)

Описывает то, что уже определено схемой и кодом; разделы про сбор сигналов
дополняются на этапах 1–2.

## Сущности и роли

- `provider` — PSP, шлюз, оркестратор или BNPL-провайдер. Роли из ТЗ:
  `gateway`, `acquirer`, `orchestrator`, `bnpl`, `wallet`, `fraud_tool`,
  `3ds_sdk` (две последние — только для C1/C2).
- `payment_method` — таксономия 5.3: карты (по схеме), кошельки, BNPL,
  банковские переводы/редиректы, прямой дебет, наличные/ваучеры, локальные
  методы.
- `platform` — платформа магазина с иерархией (`woocommerce → wordpress`).
- `vertical` — 20 вертикалей.

## Правила детекции

Правило (`rules/**/*.yaml`): `id`, `version`, `target_type`, `target_id`,
`signal_type` (`network_host`, `script_src`, `js_global`, `html_pattern`,
`cookie`, `header`, `iframe_src`, `meta`, `sha256`), `pattern`, `weight` (0–1),
`page_scope`, `enabled`, `needs_verification`, `source` (ссылка на
официальную документацию), `note`.

Инварианты, проверяемые загрузчиком:
- `id` начинается с `{target_type}.{target_id}.`;
- `target_id` существует в справочнике (FR-NR-05);
- `enabled: true` несовместимо с `needs_verification: true`;
- регулярные выражения компилируются, хосты валидны, sha256 — 64 hex;
- версия ruleset = хеш содержимого всех правил; изменение паттерна без
  повышения `version` отклоняется при синхронизации.

Сигнатуры берутся только из официальной документации вендора (ADR-0004).
Всё, в чём нет уверенности, лежит как `enabled: false, needs_verification: true`
и перечислено в отчёте этапа.

## Уверенность (FR-DT-04)

Уровни `high | medium | low` и число `confidence_score ∈ [0, 1]`. Скоринг
по весам правил и правило «два подряд» для истории реализуются на этапе 2;
схема (`store_provider.confidence`, `confirmations`, `misses`) готова.

## Gold set и метрики (FR-QA-01/02)

Gold set — ручная разметка по домену: `present=true|false` для провайдера,
метода, платформы и страны. Разметка никогда не генерируется кодом
(ADR-0006); в репозитории только синтетическая выборка `*.example` для
тестов метрик.

Метрики: по типу сущности и по правилу.
- provider / payment_method — множественные метки: TP, FP, FN по парам
  (домен, сущность); отрицательные метки (`present=false`) превращают
  предсказание в FP.
- platform / country — единственное значение на домен.
- precision = TP/(TP+FP), recall = TP/(TP+FN), F1 — гармоническое среднее.
- Per-rule метрики читаются из ClickHouse (`obs_provider`, `obs_payment_method`,
  `obs_tech`): правило → сколько его срабатываний подтверждено разметкой.
- Gate: precision по PSP ≥ 0.95 (настройка `quality.min_psp_precision`);
  пустой gold set → «not applicable», релиз не блокируется, но и не
  подтверждается.

## Ограничения сканера (неизменяемые)

Не нажимать кнопки оплаты/заказа; не вводить реальные платёжные данные;
только значения из `reference/test_payment_values.yaml` под
`allow_payment_field_fill`; регистрация только по FR-CW-12 под флагом; без
CAPTCHA, stealth и ротации IP; соблюдение robots.txt; собственный User-Agent
`PayIntelBot/1.0 (+<страница бота>)`. Реализуется отдельным слоем guardrails
на этапе 2 с тестом на «магазине-ловушке» (AC-04).

## Поля C1 (AS-23)

`store_profile.platform_version`, `obs_tech.version`, категории сторонних
хостов кроме платёжных, роли `fraud_tool`/`3ds_sdk` — внутренние. Они не
входят в клиентские Pydantic-схемы ответов и выгрузок (этап 3, тест AC-07).
