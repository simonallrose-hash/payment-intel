# Сигнатуры Wappalyzer / webappanalyzer (FR-LS-05, ADR-0005)

Каталог намеренно без данных: лицензия форка `webappanalyzer` должна быть
проверена юристом до включения его сигнатур в репозиторий. Платформы
магазинов детектируются собственными правилами в `rules/platforms/`,
составленными по официальной документации вендоров.

Загрузчик формата есть (`src/payintel/detect/wappalyzer.py`):

```
payintel rules-import-wappalyzer technologies.json --out rules/wappalyzer --source "<url/commit>"
```

Конвертер переводит поля `scriptSrc`, `js`, `headers`, `cookies`, `html`, `meta`
в типы сигналов `script_src`, `js_global`, `header`, `cookie`, `html_pattern`
(схема `rules/schema/rule.schema.json`), снимает теги `\;version:` /
`\;confidence:` (confidence → вес), сопоставляет имя технологии с платформой
справочника (`reference/platforms.yaml`, имя или алиас) или создаёт цель
`tech:<slug>`. Поля `dom`, `css`, `xhr`, `url`, `dns`, `robots`, `certIssuer`
по сохранённому скану не вычисляются и пропускаются (счётчик в выводе).

Все импортированные правила записываются `enabled: false,
needs_verification: true` (правило 8 брифа); детектор читает только
`rules/providers`, `rules/methods`, `rules/platforms`. Проверенный файл
переносится в `rules/platforms/` вручную вместе с заметкой о лицензии.
