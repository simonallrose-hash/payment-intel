# ADR

Решения по неясностям ТЗ, принятые без уточняющих вопросов (консервативный вариант).

- [ADR-0001](0001-jurisdiction-and-data-residency.md) — Юрисдикция и место хранения данных
- [ADR-0002](0002-scale-first-version-and-module-a.md) — Масштаб первой версии и модуль A
- [ADR-0003](0003-payment-field-values.md) — Значения для полей оплаты (FR-CW-14)
- [ADR-0004](0004-psp-signatures-from-official-docs.md) — Сигнатуры PSP только из официальной документации
- [ADR-0005](0005-wappalyzer-license.md) — Сигнатуры Wappalyzer
- [ADR-0006](0006-gold-set-not-invented.md) — Gold set не выдумывается
- [ADR-0007](0007-no-email-alerts.md) — Алерты: Telegram и webhook, без email
- [ADR-0008](0008-account-registration.md) — Регистрация аккаунтов в магазинах (FR-CW-12)
- [ADR-0009](0009-clickhouse-denormalized-observations.md) — Денормализация наблюдений в ClickHouse
- [ADR-0010](0010-light-worker-egress-and-persistence.md) — Лёгкий сканер: egress «resolve → connect» и персистентность в потоках
- [ADR-0011](0011-light-scan-scope-and-third-party-scripts.md) — Объём лёгкого скана, сторонние скрипты и обратная связь ссылок
- [ADR-0012](0012-provider-expansion-and-signature-tiers.md) — Расширение справочника PSP и ярусы достоверности сигнатур
- [ADR-0013](0013-checkout-guardrails-architecture.md) — Архитектура guardrails чекаута (FR-CW-04, AC-04)
- [ADR-0014](0014-checkout-walk-decisions.md) — Решения по проходу до чекаута (FR-CW-02…14)
- [ADR-0015](0015-checkout-state-and-change-detection.md) — Текущее состояние по чекауту и детекция изменений (FR-DT-05/06/11, FR-HI-02…04)
- [ADR-0016](0016-api-entitlements-and-suppression.md) — API /v1: entitlements, сегменты, 403 vs 404, подавление малых ячеек
- [ADR-0017](0017-portal-security.md) — Портал и админка: серверные сессии, CSP без inline, без HTMX/Tailwind с CDN
- [ADR-0018](0018-exports-watermark-canaries.md) — Экспорты: водяной знак, канарейки, лимиты и формат
- [ADR-0019](0019-alerts-delivery.md) — Алерты: доставка, подпись, ретраи, дайджесты
- [ADR-0020](0020-admin-rule-versions-and-preview.md) — Правила детекции в админке: версии, наложение, предпросмотр
