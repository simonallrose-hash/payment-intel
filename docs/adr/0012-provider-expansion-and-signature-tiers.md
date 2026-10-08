# ADR-0012: Расширение справочника PSP и ярусы достоверности сигнатур

Статус: принято (между этапами 1 и 2, 2026-10-08)

## Контекст

Стартовый справочник этапа 0 содержал 45 провайдеров, в основном
европейских и американских шлюзов. Заказчик попросил (08.10.2026) добавить
самые используемые PSP, расширить список и сделать сигнатуры «уникальнее»:
стартовые правила часто опирались на общий суффикс хоста или короткое имя
глобала, что даёт ложные срабатывания на тег-менеджерах и общих CDN.
Правило ADR-0004 остаётся: сигнатуры берутся только из официальной
документации вендора, всё остальное лежит как `needs_verification`.

## Решение

1. **Справочник расширен до 135 провайдеров** (`reference/providers.yaml`,
   блок «Expansion 2026-10-08»): глобальные (Nuvei, Airwallex, Paddle,
   BlueSnap, dLocal, EBANX, Bolt, 2Checkout, Chargebee, Recurly, FastSpring),
   Северная Америка (NMI, Clover, Moneris, Helcim, Stax, Mastercard Gateway),
   Европа (PayPlug, HiPay, Lyra/PayZen, Alma, Scalapay, Satispay, Axerve,
   PAYCOMET, Datatrans, wallee, Payrexx, PostFinance, Trust Payments, Opayo,
   Dojo, Judopay, Ecommpay, Fondy, Tpay, Paysera, Paytrail, Vipps MobilePay,
   Dintero, Bambora, Trustly, GoCardless, Revolut Pay, Skrill),
   Латинская Америка (Conekta, Openpay, Kushki, Wompi, Culqi, PlacetoPay,
   Getnet, PagBank, Cielo, Pagar.me), Ближний Восток и Африка (Tap, PayTabs,
   Moyasar, Tabby, Tamara, Paymob, HyperPay, Network International, Paystack,
   Flutterwave, Yoco, Peach, Payfast, Ozow), Азия и Океания (Omise, Xendit,
   Midtrans, 2C2P, PayMongo, Toss, GMO-PG, SBPS, KOMOJU, Paidy, Cashfree,
   Paytm, PhonePe, Juspay, CCAvenue, eWAY, Pin, Windcave, Zip, Sezzle),
   плюс WooPayments как white-label Stripe. Дочерние бренды связаны через
   `parent_provider_id` (Clover → Fiserv, Skrill → Paysafe, Opayo → Elavon,
   Bambora → Worldline, Paytrail → Nexi, Paidy → PayPal, eWAY → Global
   Payments, WooPayments → Stripe, Payfast → Network International).
   Роли: биллинговые платформы (Chargebee, Recurly, Juspay, Bolt) —
   `orchestrator`; merchant-of-record (Paddle, 2Checkout, FastSpring) —
   `gateway`; open-banking провайдеры (Trustly, GoCardless, Ozow) —
   `local_method_provider`.
2. **Ярусы сигнатур.** Для каждого провайдера правила строятся по убыванию
   уникальности, и вес отражает её:
   - полный путь SDK (`script_src`, например
     `cdn.safecharge.com/safecharge_resources/v1/websdk/safecharge.js`) —
     0.6–0.7;
   - уникальное имя глобала (`PaystackPop`, `ConektaCheckoutComponents`,
     `SecureTrading`) — 0.5–0.6; общие имена (`snap`, `Checkout`, `P`,
     `CardSDK`) — ≤0.2 и всегда `needs_verification`;
   - хост платёжной страницы или API, который виден только из чекаута
     (`page_scope: checkout`) — 0.5–0.6;
   - маркер разметки (`<scalapay-widget`, `data-cb-site=`,
     `id="bolt-embed"`) — 0.4–0.5;
   - широкий суффикс (`*.vendor.com`) — 0.3–0.4;
   - песочницы и staging — 0.2 с `note`.
3. **Что включено, а что нет.** Включены только правила, чей паттерн
   дословно присутствует в официальной публикации вендора, указанной в
   `source`. Официальной публикацией считаются: страница документации
   вендора (в том числе её `.md`-версия или `llms.txt`), официальный
   npm-пакет вендора, репозиторий вендора на GitHub и живой файл SDK,
   отданный с хоста вендора. При первом проходе документация 45 вендоров
   не открывалась напрямую; при самопроверке источники для 40 из них найдены
   обходными путями, и подтверждённые правила включены (`source` дополнен
   ссылкой после `verified:`, `version` поднят). Для 5 вендоров (2C2P, Dojo,
   Payfast, SB Payment Service, Yoco) ни один официальный источник не
   удалось получить (robots.txt на всех хостах, порталы только на
   JavaScript, документация за логином); их правила остаются
   `enabled: false, needs_verification: true` с причиной в `note`. Итого
   700 правил, из них 119 под проверкой: серверные API-хосты, которые нужно
   подтверждать на реальном чекауте, песочницы и устаревшие хосты без
   упоминания в текущих документах, общие имена глобалов.
4. **Существующие 45 провайдеров не переписывались**: их правила остались
   версией 1, чтобы gold set и метрики этапа 0 не сдвинулись; уточнение
   широких правил (например `*.worldline-solutions.com`) идёт через обычную
   процедуру из `needs-verification` списка.

## Последствия

Справочник покрывает ключевые рынки целевой географии (весь мир без СНГ и
стран с низким доходом, ADR по ТЗ 08.10.2026). Порог FR-DT-03 (≥30
провайдеров) перевыполнен. Список `needs_verification` после самопроверки
содержит 119 правил; его разбор — работа аналитика и этапа 2 (перехватчик
сети подтвердит или снимет серверные хосты): до подтверждения эти правила
не участвуют в детекции и не влияют на точность. Тесты, которые фиксируют
размер справочника (`+226`, `700`), обновлены.
