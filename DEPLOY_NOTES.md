# DEPLOY_NOTES — TaskFlow Bot

> Дата: 2026-04-18
> Сервер: `109.120.138.241` (Hetzner/Aeza, Ubuntu 24.04, Docker 29.3.1, Compose v5.1.1)
> Путь: `/root/taskflow_bot`

---

## А. Что сделано (короткая сводка)

**Stack:** три контейнера в отдельной bridge-сети `taskflow_net`.

| Компонент | Контейнер | Образ | Volume | Порты наружу |
|---|---|---|---|---|
| Бот | `taskflow_bot` | `taskflow_bot-bot` (локальная сборка из Dockerfile) | — | нет |
| PostgreSQL 16 | `taskflow_postgres` | `postgres:16-alpine` | `taskflow_pg_data` | нет |
| Redis 7 | `taskflow_redis` | `redis:7-alpine` | `taskflow_redis_data` | нет |

**Сеть:** `taskflow_net` (bridge). Бот ходит в БД по DNS-имени `postgres:5432`, в кэш — `redis:6379`. Соседние сети (`proxy-net`, `elina_elina_net`) **не затронуты**.

**Файлы локально:** `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.example`, `DEPLOY_NOTES.md`.
**Файлы на сервере:** вся кодовая база в `/root/taskflow_bot`, плюс созданный на месте `/root/taskflow_bot/.env` с правами `600`.

**Состояние контейнеров:** все три в статусе `Created` (не запущены). Образ бота собран, PG и Redis образы скачаны.

**Пароль PostgreSQL:** сгенерирован случайно (32 символа, alpha+digits), записан в **`/root/taskflow_bot/.env`** на сервере в двух местах синхронно:
- `POSTGRES_PASSWORD=<значение>`
- `PG_DSN=postgresql://taskflow:<значение>@postgres:5432/taskflow`

Локально пароль не хранится. Посмотреть можно на сервере: `grep POSTGRES_PASSWORD /root/taskflow_bot/.env`.

**Что НЕ тронуто у соседей:**
- `elina`, `3x-ui`, `nginx-proxy` — работают, статус `Up` как и был.
- Сеть `proxy-net` — не подключались, не изменялись.
- Volumes `elina_elina_*` — не трогали.

---

## Б. Ручные шаги для старта

### Б.1. Заполнить секреты в `.env`

```bash
ssh root@109.120.138.241
cd /root/taskflow_bot
nano .env
```

Заполнить пустые значения (всё остальное уже стоит):

- `BOT_TOKEN=` → токен от BotFather
- `AIRTABLE_API_KEY=` → ключ Airtable
- `AIRTABLE_BASE_ID=` → id базы
- `AIRTABLE_TABLE=Итерация` → поменять только если таблица переименована
- `GROUP_ID=` → id рабочей группы XX1.3 (для PROD; в TEST_MODE не используется, но лучше уже положить)
- `MODERATOR_GROUP_ID=` → id группы модераторов
- `MODERATOR_IDS=123,456` → через запятую, без пробелов
- `TEST_USER_ID=` → твой Telegram id

**Не трогать:** `TEST_MODE=true` (для первого прогона), `POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_PASSWORD`, `PG_DSN`, `REDIS_URL`.

### Б.2. Поднять PG и Redis, дождаться зелёных healthcheck'ов

```bash
# terminal A
docker compose start postgres redis

# terminal B (в параллели, следить)
docker compose logs -f postgres redis
```

PG поднимается ~3-5 сек (первый запуск — инициализация пустой БД). Признак готовности:
- `taskflow_postgres` → `database system is ready to accept connections` (в логе) и `(healthy)` в `docker compose ps`.
- `taskflow_redis` → `Ready to accept connections` и `(healthy)`.

Проверка статуса:
```bash
docker compose ps
```

### Б.3. Стартовать бота

```bash
docker compose start bot
docker compose logs -f --tail=100 bot
```

**Что смотреть в логах:**
- `PostgreSQL pool initialized` — подключение к PG удалось.
- `PostgreSQL schema ensured` — миграции отработали (таблицы tasks, pending_results, settings + колонки result_content/result_type/result_received_at из СР-2).
- `Redis connected` — подключение к Redis.
- `Бот запущен (TEST_MODE=True, MODERATOR_GROUP_ID=..., DB=PostgreSQL, Cache=Redis)` — всё ок.
- `[scheduler] Запущен: check_stale=1min, check_pending_moderation=10min` — планировщик жив.

**Если всплывает при отправке медиа** (разово, ожидаемо):
- `Медиа-ссылки в Airtable содержат BOT_TOKEN ...` — WARNING по СР-4, показывается один раз.

### Б.4. Ручной сценарий тестирования

Из TEST_MODE-сценария, в двух окнах (ЛС с ботом + группа модераторов):

1. `/start` в ЛС.
2. `/панель` → должна прийти клавиатура с тумблером «🔴 Публикация: ВЫКЛ» (по умолчанию выключено — СР-5).
3. Нажать «🔴 Публикация: ВЫКЛ» → должна стать «🟢 Публикация: ВКЛ».
4. Нажать «📥 Загрузить задачи» → ввести `1` → бот пришлёт карточку с кнопкой **«✅ Опубликовать»** (КР-1: в TEST_MODE тут именно «Опубликовать», не «Взять»).
5. Нажать «✅ Опубликовать» → в группе модераторов появится сообщение с префиксом `[🧪 TEST MODE]` и кнопкой **«👤 Взять задачу (тест)»**.
6. Нажать «👤 Взять задачу (тест)» → в ЛС придёт «Задача #N взята».
7. Написать в ЛС текст-результат → в группу модераторов придёт карточка «📨 Результат по задаче #N» + пересланное сообщение + кнопки `[✅ Принять] [❌ Не принять]`.
8. Нажать «✅ Принять» → в ЛС «Результат принят», в Airtable статус должен перейти в «Завершено».
9. Повторить 4-7, но на шаге 8 нажать «❌ Не принять» → ввести причину → в ЛС придёт отклонение.
10. **Проверка неподдерживаемого типа:** взять новую задачу (4-6), в ЛС прислать контакт / геолокацию / GIF — бот должен ответить «Этот формат не поддерживается...» и **не** пересылать модераторам.
11. `/test_skip_stale` → должно прийти уведомление о простое в группу модераторов с кнопкой «🔁 Да, опубликовать повторно».
12. На этом уведомлении сначала **выключи публикацию** тумблером (шаг 3 наоборот) и нажми «🔁 Да, опубликовать повторно» → должен прийти alert «Публикация выключена глобально» (правка на повторную публикацию).
13. Включи публикацию → снова нажми «🔁 Да, опубликовать повторно» → задача переопубликуется, кнопка «👤 Взять задачу (тест)» на новом сообщении должна быть (КР-3).
14. `/test_status` → показывает загруженные/активные задачи и исполнителей.
15. `/test_reset` → очищает БД задач и Redis-кэш.

### Б.5. Переключить бота на автозапуск

После успешного прогона:

```bash
cd /root/taskflow_bot
# на сервере отредактировать docker-compose.yml:
sed -i 's/restart: "no"/restart: unless-stopped/' docker-compose.yml
# применить изменение:
docker compose up -d bot
docker compose ps
```

### Б.6. Боевой режим (после «ОК» от тебя)

```bash
cd /root/taskflow_bot
nano .env            # TEST_MODE=true → TEST_MODE=false
docker compose restart bot
docker compose logs -f --tail=50 bot
```

Плюс добавить бота в группу **XX1.3** (рабочая группа исполнителей) с правами администратора и включёнными реакциями (см. раздел «Требования к группе XX1.3» в README.md).

---

## В. Повседневные команды

```bash
cd /root/taskflow_bot

# Статус
docker compose ps

# Логи
docker compose logs -f --tail=200 bot
docker compose logs -f --tail=100 postgres
docker compose logs -f --tail=100 redis

# Пересборка и перезапуск бота после правки кода
#   (на локальной машине — отредактировать код, затем:)
#   rsync -avz --delete --exclude='venv/' --exclude='__pycache__/' \
#         --exclude='.env' --exclude='data/' --exclude='logs/' \
#         --exclude='.git/' ./ root@109.120.138.241:/root/taskflow_bot/
docker compose build bot && docker compose up -d bot

# Бэкап PG (запускать на сервере)
source .env && docker exec taskflow_postgres \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > backup_$(date +%F).sql

# Остановить всё
docker compose stop

# Остановить и удалить контейнеры (данные в volume сохранятся)
docker compose down

# ВНИМАНИЕ: снести volumes (ТЕРЯЕТ ДАННЫЕ БД и Redis)
docker compose down -v
```

---

## Г. Типовые проблемы

### Г.1. Бот не может подключиться к PG: `password authentication failed`

**Симптом:** `taskflow_bot` падает с `InvalidPasswordError` в логах.
**Причина:** расхождение между `POSTGRES_PASSWORD` и паролем в `PG_DSN`. При генерации клали в оба места, но при ручной правке легко разойтись.
**Починка:**
```bash
grep -E '^(POSTGRES_PASSWORD|PG_DSN)=' .env
# пароль в PG_DSN между `taskflow:` и `@postgres` должен 1:1 совпадать с POSTGRES_PASSWORD
# если разошлись — поправь одновременно, потом:
docker compose restart bot
```

### Г.2. `taskflow_postgres` не переходит в `healthy`, цикл рестартов

**Симптом:** `docker compose ps` показывает `taskflow_postgres` как `unhealthy` или в бесконечном healthcheck.
**Причина 1:** volume `taskflow_pg_data` был инициализирован с другим паролем и теперь не пускает нового `POSTGRES_USER`. PG инициализирует БД **только при первом запуске пустого volume** и потом игнорирует переменные окружения для пароля.
**Починка (ТЕРЯЕТ ДАННЫЕ в PG):**
```bash
docker compose stop postgres
docker volume rm taskflow_pg_data
docker compose up --no-start postgres
docker compose start postgres
```

### Г.3. `taskflow_bot` стартанул, но `RuntimeError: Missing required env var: BOT_TOKEN`

**Симптом:** контейнер крутится в `restart loop` с тем же сообщением в логах.
**Причина:** пустое значение в `.env` (config.py строго требует BOT_TOKEN/AIRTABLE_API_KEY/AIRTABLE_BASE_ID/GROUP_ID/MODERATOR_GROUP_ID).
**Починка:** `nano .env`, заполнить, `docker compose restart bot`.

### Г.4. Бот не видит группу (`chat not found` / `bot was kicked`)

**Симптом:** в логах `aiogram.exceptions.TelegramBadRequest: Bad Request: chat not found` при старте или в момент первой публикации.
**Причина:** `MODERATOR_GROUP_ID` (или `GROUP_ID`) неверный. Для супергрупп ID выглядит как `-100...`, для обычных групп — отрицательное целое без `-100` префикса. Бот должен быть **уже добавлен** в чат — aiogram не «находит» чат, если бота там нет.
**Починка:** добавить бота в группу, проверить ID через `@RawDataBot`, поправить `.env`, рестарт.

### Г.5. Healthcheck у PG или Redis вечно `starting`

**Симптом:** `taskflow_postgres` или `taskflow_redis` в `Created` после `docker compose start`, или в `(health: starting)` больше минуты.
**Причина:** PG в первый раз инициализирует БД (`initdb`) — может занять 5-10 сек на слабом диске. Healthcheck запускается в момент контейнера, первые несколько секунд он fail'ится нормально.
**Что делать:** подождать 30 сек и пересмотреть `docker compose ps`. Если через минуту всё ещё `starting` — `docker compose logs postgres` покажет, в чём дело.

### Г.6. Redis потерял `stale_notified`/`result_text` после рестарта

**Симптом:** бот после рестарта заново рассылает уведомления о простое по тем же задачам или забывает контент результата.
**Причина:** `appendonly yes` включён, volume `taskflow_redis_data` смонтирован — потери быть не должно. Если это всё же случилось — volume был пересоздан.
**Дополнительная защита:** результаты дублируются в PG (`pending_results.result_content`) — см. СР-2; при старте `run.py` прогревает Redis из PG. То есть `result_text` переживёт потерю Redis; `stale_notified` — нет, но это максимум приведёт к одному лишнему уведомлению.

---

*Файл лежит также на сервере: `/root/taskflow_bot/DEPLOY_NOTES.md`. Пароль PG в этот файл НЕ помещён — читай его из `/root/taskflow_bot/.env` командой `grep POSTGRES_PASSWORD /root/taskflow_bot/.env`.*
