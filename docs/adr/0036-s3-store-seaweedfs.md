# ADR-0036: S3-хранилище — SeaweedFS вместо MinIO

Статус: принято (долг 5, 2026-10-12). Связано: AS-16, NFR-S-07, LR-08,
FR-LS-04, FR-CW-08, NFR-P-06, ADR-0034.

## Контекст

ТЗ (AS-16) называет S3-совместимое хранилище для артефактов и выгрузок, и
до этого решения им был MinIO: `docker-compose.yml`, `docker-compose.dev.yml`,
тесты (`tests/conftest.py`) и ночной `bench.yml` ссылались на
`minio/minio:RELEASE.2024-12-18T13-15-44Z` и `bitnami/minio:2024`.

12.10.2026 первый ночной запуск `bench.yml` упал на шаге «Initialize
containers»: `docker pull bitnami/minio:2024` → «Docker pull failed».
Проверка (все наблюдения — 12.10.2026, 13:30–14:00 UTC):

- `bitnami/minio` на Docker Hub больше не имеет тегов (Bitnami перенёс
  образы в замороженный `bitnamilegacy/minio`, последняя сборка 03.07.2025).
- `minio/minio`: API тегов Docker Hub отвечает «object not found», анонимный
  токен на `pull` отвергается реестром (UNAUTHORIZED), `docker pull` →
  «repository does not exist or may require 'docker login'». MinIO с
  октября 2025 распространяет только исходный код («source-only
  distribution»), `quay.io/minio` требует входа. Образ остаётся только в
  локальных кэшах; свежий стенд его не получит.

Нужна замена с версионными публичными образами, поддерживающая то, что
использует `payintel.core.s3.ObjectStore`: `head/create bucket`, `put/get/
delete object`, `delete_objects`, lifecycle-правила (`Expiration` по префиксу,
LR-08), presigned GET (FR-EX-04) и SSE-S3 (NFR-S-07).

## Рассмотренные варианты

Каждый кандидат прогнан скриптом, вызывающим все методы `ObjectStore`
(бакеты, put/get 1 КБ и 8 МБ, lifecycle put/get, presigned GET через
`urllib`, delete, повторный get) плюс put с `ServerSideEncryption=AES256`.

| | SeaweedFS 4.45 | Garage v2.4.1 | RustFS 1.0.1 |
| --- | --- | --- | --- |
| Лицензия, возраст | Apache-2.0, с 2014 г. | AGPL-3.0, с 2021 г. | Apache-2.0, релиз 1.0 — октябрь 2026 |
| Образ (Docker Hub) | `chrislusf/seaweedfs:4.45` (714 МБ), теги по версиям | `dxflrs/garage:v2.4.1` (99 МБ) | `rustfs/rustfs:1.0.1` (405 МБ) |
| Операции `ObjectStore` | все прошли | все прошли при `s3_region` в конфиге = регион клиента; иначе 400 на `HeadBucket` и presigned | все прошли |
| SSE-S3 (`AES256`) | работает: заголовок возвращается в `PutObject`/`HeadObject` | **не реализовано** — заголовок молча игнорируется, объект хранится открыто | требует `RUSTFS_SSE_S3_MASTER_KEY` (иначе `InvalidRequest`) |
| Lifecycle `Expiration` | принимается; в 4.x исполняется отдельным worker'ом по расписанию admin-сервера (`weed admin` + `weed worker`), по умолчанию раз в сутки | принимается и исполняется | принимается; в README помечено «Under Testing» |
| Запуск | один процесс `weed server -s3` | нужен `garage.toml`, в single-node режиме ключ и бакет из `GARAGE_DEFAULT_*` | один процесс, порт 9000 как у MinIO |

## Решение

1. **SeaweedFS 4.45** (`chrislusf/seaweedfs:4.45`) — S3-хранилище стенда,
   dev-окружения, тестов и bench. Сервис в compose называется `s3`
   (профиль `data-1`), том `s3-data`, команда
   `server -s3 -s3.port=9000 -s3.port.iceberg=0 -s3.port.lance=0 -dir=/data
   -ip.bind=0.0.0.0 -master.telemetry=false`: порт S3 тот же, что был у
   MinIO (9000, правила файрвола не меняются), каталоги Iceberg/Lance
   выключены, телеметрия выключена. Учётная запись администратора S3 —
   переменные `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` контейнера
   (`S3_ACCESS_KEY`/`S3_SECRET_KEY` в `.env`, те же значения, что
   `PAYINTEL_S3__ACCESS_KEY`/`SECRET_KEY`). Остальные слушатели SeaweedFS
   (master 9333, volume 8080, filer 8888 и их gRPC) остаются внутри сети
   `data`. Healthcheck — `GET /healthz` на порту S3.
2. **Удаление сырых артефактов через 90 дней (LR-08) не зависит от
   хранилища.** Правило lifecycle на бакет по-прежнему ставится при старте
   воркеров (`ObjectStore.ensure_lifecycle`), но исполняет срок сам
   `payintel`: `ObjectStore.expire_prefix` удаляет объекты префиксов
   `light/` и `checkout/` старше `retention.raw_artifacts_days`
   (`list_objects_v2` постранично, `delete_objects` пачками по 1000);
   команда `payintel artifacts sweep [--once] [--dry-run]`, и тот же проход
   раз в сутки делает `exporter` (`exports run --retention-interval`).
   Так срок соблюдается и там, где lifecycle не исполняется (admin/worker
   SeaweedFS не подняты, другое хранилище), и проверяется тестом.
3. В GitHub Actions SeaweedFS запускается шагом `docker run` (сервис-контейнеры
   не принимают команду), в тестах — testcontainers с тем же образом и
   командой; образ можно подменить `PAYINTEL_TEST_S3_IMAGE`, внешнее
   хранилище — `PAYINTEL_TEST_S3_ENDPOINT`.
4. Бэкап артефактов и выгрузок (runbook §7) — `rclone sync` в S3 другого
   провайдера вместо `mc mirror`; проверка lifecycle — `aws s3api
   get-bucket-lifecycle-configuration` или `payintel artifacts sweep
   --dry-run` вместо `mc ilm ls`.

## Последствия

- Работающий стенд с MinIO из кэша продолжает работать; перенос —
  `rclone sync` из старого бакета в новый сервис `s3`, затем переключение
  `PAYINTEL_S3__*` (runbook §18, п. 6). Том `minio-data` не переиспользуется:
  формат хранения другой.
- Garage отвергнут из-за отсутствия шифрования на стороне сервера: NFR-S-07
  закрывалось бы только шифрованием диска, а код считал бы заголовок SSE
  принятым. RustFS — из-за возраста: первый стабильный релиз вышел в тот же
  месяц, lifecycle не объявлен готовым; вернуться к нему можно без изменений
  кода (тот же порт и протокол).
- Образ SeaweedFS тяжелее MinIO (714 МБ против ~170 МБ) и поднимает четыре
  компонента в одном процессе; на 1 млн магазинов это не сказывается
  (bench P-06 идёт через тот же `ObjectStore`).
- AS-16 в матрице трассировки читается как «S3-совместимое хранилище»;
  название продукта — решение этого ADR, а не ТЗ.
