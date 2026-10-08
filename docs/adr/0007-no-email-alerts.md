# ADR-0007: Алерты: Telegram и webhook, без email

Статус: принято (этап 0, 2026-10-08)

## Контекст

UC-08 и описание пакета `alerts/` упоминают email, тогда как AS-19 и
FR-AL-04 определяют Telegram и webhook.

## Решение

Реализуются Telegram и webhook (`webhook`, `delivery` в схеме). Email не
реализуется; SMTP-настроек в `settings.py` нет.

## Последствия

Меньше поверхности для утечек и секретов; email можно добавить отдельным
ADR.
