# TaskFlow Bot — Operations Guide

> Живой справочник по эксплуатации бота: где что лежит, как обновить код, как смотреть логи, как бэкапить, что делать если что-то сломалось.

---

## 0. Quick reference

| Артефакт | Значение |
|---|---|
| VPS | `109.120.138.241` (Hetzner/Aeza, Ubuntu 24.04) |
| Хостнейм | `eagergreen.aeza.network` |
| Локальный путь (Windows) | `C:\Users\Admin\Desktop\TaskFlow\taskflow_scheduler_bot\` |
| VPS путь | `/home/claude/taskflow_bot` |
| VPS пользователь | `claude` (uid 1000, в группе `docker`) |
| Git remote | `git@github.com:Talooren/taskflow_scheduler_bot.git` |
| GitHub deploy key на VPS | `/home/claude/.ssh/id_ed25519_taskflow` (read-only) |
| `.env` с живым PG-паролем | **только на VPS**, в `/home/claude/taskflow_bot/.env` (600 прав). Бэкапы — `/home/claude/.env.backup.<timestamp>` |
| Контейнеры | `taskflow_bot`, `taskflow_postgres`, `taskflow_redis` |
| Сеть Docker | `taskflow_net` (bridge, только внутренняя) |
| Named volumes | `taskflow_pg_data`, `taskflow_redis_data` |
| Порты наружу | **нет** (бот — long-polling Telegram) |

---

## 1. Подключение к VPS

### С iPhone через Termius
| Поле | Значение |
|---|---|
| Hostname | `109.120.138.241` |
| Port | `22` |
| Username | `root` или `claude` |
| Auth | пароль либо SSH-ключ |

После входа как root — переключиться на `claude` для всех операций с ботом:
```bash
sudo -iu claude
cd ~/taskflow_bot
```

Если зашёл сразу как `claude` — ты уже в нужном shell, просто `cd ~/taskflow_bot`.

### С Windows через git bash / PowerShell
```bash
ssh root@109.120.138.241
# или
ssh claude@109.120.138.241
```

---

## 2. Ежедневный workflow: правишь код локально → выкатываешь на VPS

### Локально (Windows, в корне проекта)
```bash
cd C:/Users/Admin/Desktop/TaskFlow/taskflow_scheduler_bot

# посмотреть что изменилось
git status
git diff

# закоммитить и запушить
git add .
git commit -m "описание изменений"
git push
```

### На VPS (под пользователем `claude`)
```bash
cd ~/taskflow_bot
git pull
```

**Если менялся Python-код / Dockerfile / requirements.txt — пересобрать образ бота:**
```bash
docker compose build bot
docker compose up -d bot
docker compose logs -f --tail=100 bot
```

**Если менялся только README / .md / .env.example — ничего делать не надо**, контейнер работает как был.

**Если менялся `docker-compose.yml` (добавлен сервис, volume, изменён healthcheck):**
```bash
docker compose up -d
```
(без `--build` — Compose сам решит, что нужно пересоздать).

---

## 3. Холодный старт бота (первый раз или после `docker compose down`)

Только если контейнеры в `Created` / `Exited`, а не `Up`. Проверка: `docker compose ps`.

```bash
cd ~/taskflow_bot

# 1. заполнить .env если ещё не заполнен
nano .env
# нужны:
#   BOT_TOKEN, AIRTABLE_API_KEY, AIRTABLE_BASE_ID
#   GROUP_ID, MODERATOR_GROUP_ID, MODERATOR_IDS, TEST_USER_ID
# НЕ ТРОГАТЬ:
#   POSTGRES_*, PG_DSN (уже заполнено, пароль сгенерирован один раз)
#   REDIS_URL

# 2. поднять PG и Redis, дождаться (healthy)
docker compose start postgres redis
docker compose logs -f postgres redis  # жди "ready to accept connections"

# 3. поднять бота
docker compose start bot
docker compose logs -f --tail=100 bot
# ищем в логах:
#   "PostgreSQL pool initialized" — PG ок
#   "Redis connected" — кэш ок
#   "Бот запущен (TEST_MODE=True, ..., DB=PostgreSQL, Cache=Redis)" — всё хорошо
#   "[scheduler] Запущен: check_stale=1min, check_pending_moderation=10min" — джобы крутятся
```

---

## 4. Мониторинг

### Статус контейнеров
```bash
cd ~/taskflow_bot
docker compose ps
# ожидание: три контейнера с статусом "Up" и health "healthy" у postgres/redis
```

### Логи
```bash
# бот
docker compose logs -f --tail=200 bot

# PG
docker compose logs -f --tail=100 postgres

# Redis
docker compose logs -f --tail=100 redis

# все разом
docker compose logs -f --tail=50
```

Выход — Ctrl+C.

### Нагрузка контейнеров
```bash
docker stats --no-stream | grep -E 'taskflow_|CONTAINER'
```

### Содержимое PG вживую (проверить что пишет бот)
```bash
# все активные задачи
docker exec -i taskflow_postgres psql -U taskflow -d taskflow -c \
  "SELECT record_id, status, task_number, task_name FROM tasks WHERE status != 'done' ORDER BY loaded_at;"

# ожидающие результаты
docker exec -i taskflow_postgres psql -U taskflow -d taskflow -c \
  "SELECT user_id, username, task_number, result_type, result_received_at FROM pending_results;"

# настройки
docker exec -i taskflow_postgres psql -U taskflow -d taskflow -c \
  "SELECT * FROM settings;"
```

### Содержимое Redis (ключи TTL и т.п.)
```bash
docker exec -it taskflow_redis redis-cli
# внутри:
> KEYS *
> TTL stale_notified:recXXX
> GET result_text:recXXX
> EXIT
```

---

## 5. Бэкапы

### PostgreSQL — один дамп
```bash
cd ~/taskflow_bot

# читаем креды из .env и делаем pg_dump внутри контейнера
set -a; source .env; set +a
docker exec taskflow_postgres \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > ~/backup_$(date +%F_%H%M).sql

ls -la ~/backup_*.sql
```

### Ежедневный крон (опционально)
Установить `~/bin/pg_backup.sh`:
```bash
mkdir -p ~/bin ~/backups
cat > ~/bin/pg_backup.sh <<'EOF'
#!/bin/bash
set -e
cd /home/claude/taskflow_bot
set -a; source .env; set +a
mkdir -p /home/claude/backups
FILE=/home/claude/backups/taskflow_$(date +%F_%H%M).sql
docker exec taskflow_postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > "$FILE"
find /home/claude/backups -name 'taskflow_*.sql' -mtime +14 -delete
EOF
chmod +x ~/bin/pg_backup.sh
```

В cron (`crontab -e` под `claude`):
```
0 3 * * * /home/claude/bin/pg_backup.sh >> /home/claude/backups/cron.log 2>&1
```

Бэкапы старше 14 дней — удаляются автоматически.

### Восстановление из дампа
```bash
cd ~/taskflow_bot
set -a; source .env; set +a
# ВНИМАНИЕ: полностью перезапишет БД
docker exec -i taskflow_postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB" < ~/backup_YYYY-MM-DD_HHMM.sql
```

---

## 6. Остановка / перезапуск

```bash
cd ~/taskflow_bot

# мягкий рестарт бота (применить новый код, например)
docker compose restart bot

# полный рестарт всех трёх
docker compose restart

# остановить всё (данные в volumes — сохранятся)
docker compose stop

# стоп + удалить контейнеры (volumes остаются)
docker compose down

# поднять из stop/down
docker compose up -d

# СТРАШНО: снести всё включая данные PG и Redis
docker compose down -v     # -v удаляет volumes
```

---

## 7. Rollback (откатить код)

### Откатить на предыдущий коммит
Локально:
```bash
cd C:/Users/Admin/Desktop/TaskFlow/taskflow_scheduler_bot
git log --oneline | head -5                      # найти нужный SHA
git revert <SHA>                                  # создать коммит-отмену (НЕ force push)
git push
```

На VPS:
```bash
cd ~/taskflow_bot
git pull
docker compose build bot && docker compose up -d bot
```

### Жёсткий откат к конкретной ревизии (если revert не подходит)
```bash
# локально
git reset --hard <SHA>
git push --force-with-lease                       # аккуратный force

# VPS
cd ~/taskflow_bot
git fetch origin main
git reset --hard origin/main
docker compose build bot && docker compose up -d bot
```

---

## 8. Типовые проблемы

### «Бот не стартует, падает с `RuntimeError: Missing required env var: BOT_TOKEN`»
В `.env` пустое обязательное поле. Открыть `nano .env`, заполнить, `docker compose restart bot`.

### «Бот падает с `InvalidPasswordError` у PG»
Рассогласование `POSTGRES_PASSWORD` и пароля внутри `PG_DSN`:
```bash
grep -E '^(POSTGRES_PASSWORD|PG_DSN)=' ~/taskflow_bot/.env
# пароль между 'taskflow:' и '@postgres' в PG_DSN должен 1:1 совпадать с POSTGRES_PASSWORD
```
Если разошлись — поправить одновременно, `docker compose restart bot`.

### «`taskflow_postgres` вечно в `(health: starting)` или `unhealthy`»
Volume `taskflow_pg_data` инициализирован под другой пароль. PG запоминает пароль при первом запуске и игнорирует env при последующих. Сброс БД:
```bash
docker compose stop postgres
docker volume rm taskflow_pg_data          # ⚠ ТЕРЯЕТ ДАННЫЕ PG
docker compose up --no-start postgres
docker compose start postgres
```

### «Бот не видит группу: `chat not found`»
ID группы в `.env` неверный. Для супергрупп — `-100...`. Проверить через `@RawDataBot`, поправить `.env`, `docker compose restart bot`.

### «`message_reaction` не приходит в продуктивную группу XX1.3»
Известная особенность Telegram: супергруппы с большим числом участников шлют агрегированный `message_reaction_count` вместо `message_reaction` с `user_id`. Бот такое событие не обрабатывает. Решения в README.md раздел «Требования к группе XX1.3».

### «`docker pull` или `git pull` падает с ошибкой прав»
Ты в неправильном shell'е (root вместо claude). Переключиться:
```bash
sudo -iu claude
cd ~/taskflow_bot
git pull
```

### «Контейнер перезапускается в цикле (Restarting)»
```bash
docker compose logs --tail=200 bot     # смотрим последние строки перед падением
```
Чаще всего — проблема в .env или сетевая (PG/Redis не отвечают). Если застрял — `docker compose stop bot`, починить причину, `docker compose start bot`.

---

## 9. Локальная dev-работа (Windows)

Если нужно прогнать бота у себя на машине (без Docker, через venv):

```bash
cd C:/Users/Admin/Desktop/TaskFlow/taskflow_scheduler_bot
# активировать venv
venv\Scripts\activate             # cmd / PowerShell
# или
source venv/Scripts/activate      # git bash

# установить зависимости если ещё нет
pip install -r requirements.txt

# создать локальный .env (не тот что на VPS — свой локальный, без прода)
cp .env.example .env
# заполнить тестовыми токенами (TEST_MODE=true, локальный SQLite-fallback если без PG)

# запустить
python run.py
```

Без `PG_DSN` / `REDIS_URL` бот автоматически откатится на SQLite + in-memory — для локальной отладки хватит.

---

## 10. Файлы в репо — что где

| Файл | Назначение |
|---|---|
| `run.py` | Точка входа, инициализация, polling |
| `app/config.py` | Чтение `.env`, валидация |
| `app/db.py` | PostgreSQL (основной) + SQLite (fallback) |
| `app/cache.py` | Redis + in-memory fallback |
| `app/airtable.py` | REST-клиент с retry (3×1/2/4с) + идемпотентный GET перед «Завершено» |
| `app/scheduler.py` | `check_stale` (1 мин) + `check_pending_moderation` (10 мин, tiers 4h/12h/24h) |
| `app/keyboards.py` | inline-клавиатуры |
| `app/handlers/moderator.py` | `/панель`, публикация, accept/reject, тумблер публикации |
| `app/handlers/executor.py` | ЛС исполнителя, медиа, отбой неподдерживаемых форматов |
| `app/handlers/reactions.py` | `message_reaction` (PROD-путь взятия задачи) |
| `app/handlers/common.py` | `/start`, `/status`, тест-команды |
| `Dockerfile` | python:3.11-slim, non-root user, WORKDIR /app |
| `docker-compose.yml` | bot + postgres + redis + taskflow_net |
| `.env.example` | шаблон для `.env` |
| `.dockerignore` | исключения для билда образа |
| `.gitignore` | исключения для репо (`.env`, `data/`, `logs/` и т.д.) |
| `README.md` | обзор проекта |
| `AUDIT_REPORT.md` | аудит кода (до докеризации) |
| `DEPLOY_RECON.md` | разведка VPS перед деплоем |
| `DEPLOY_NOTES.md` | первоначальная инструкция по деплою (устарела в части пути — сейчас `/home/claude/taskflow_bot`) |
| `OPS_GUIDE.md` | **этот файл** |
| `TODO.md` | one-liner про удаление SQLite fallback |
| `SCENARIO.md` | v1 архитектура (устарела — см. README) |

---

## 11. Ротация секретов

Если засветился BOT_TOKEN / AIRTABLE_API_KEY / пароль PG — пересоздать на стороне сервиса, потом на VPS:

```bash
cd ~/taskflow_bot
nano .env                          # поменять значение
# для PG: поменять ТОЛЬКО POSTGRES_PASSWORD — этого не хватит, нужно одновременно
#         обновить PG_DSN и пересоздать volume (см. секцию "Типовые проблемы")

docker compose restart bot         # для BOT_TOKEN / AIRTABLE_API_KEY достаточно
```

---

## 12. Если что-то непонятно

- `git log --oneline -20` в `~/taskflow_bot` на VPS или локально — история коммитов.
- `git blame <файл>` — кто и когда трогал конкретную строку.
- `docker compose logs --since=10m` — последние 10 минут логов.
- GitHub web UI: `https://github.com/Talooren/taskflow_scheduler_bot` — вся история, просмотр кода в браузере.

---

*Файл лежит в корне репо, обновляется через обычный git-flow (см. раздел 2). Автоматически синхронизируется на VPS при `git pull` под пользователем `claude`.*
