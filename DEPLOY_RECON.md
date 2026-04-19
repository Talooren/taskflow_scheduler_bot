# DEPLOY_RECON — разведка VPS 109.120.138.241

> Дата: 2026-04-18
> Хост: `eagergreen.aeza.network` (Hetzner, позиционирован как Aeza)
> Метод: SSH на root с паролем, только команды чтения. Ничего не устанавливалось, не создавалось, не изменялось.

---

## 1. Общее состояние

| Параметр | Значение |
|---|---|
| ОС | Ubuntu 24.04.4 LTS (noble), kernel 6.8.0-48-generic x86_64 |
| CPU / RAM | 2 cores / 3.8 GiB RAM + 512 MiB swap |
| Диск | 59 GiB total, **17 GiB used / 42 GiB free** на `/` |
| Uptime | 33 дня, load average 0.01 / 0.01 / 0.00 (сервер почти простаивает) |
| Docker | **29.3.1** |
| Docker Compose | **v5.1.1** (plugin, вызов `docker compose`); старого `docker-compose` нет |

**Вывод:** ресурсов более чем достаточно, всё свежее, compose-plugin стандартный.

---

## 2. Запущенные контейнеры

Всего три контейнера, все работают:

| Имя | Образ | Сеть | Порты | Restart |
|---|---|---|---|---|
| `elina` | `elina-elina` (локальная сборка) | `elina_elina_net` (172.19.0.2) | — (исходящий) | `always` |
| `3x-ui` | `ghcr.io/mhsanaei/3x-ui:latest` | `host` (всё через хост) | слушает 2053/2096 на хосте | `unless-stopped` |
| `nginx-proxy` | `nginx:1.25-alpine` | `proxy-net` (172.18.0.2) | **80, 8443** наружу | `unless-stopped` |

### 2.1 `elina` — Telegram-бот (прямой сосед по паттерну)

- Build: локальный `Dockerfile` из `/root/elina`
- Volumes (named): `elina_elina_memory → /app/memory/data`, `elina_elina_logs → /app/logs`
- Env-keys (без значений): `PROJECT_NAME`, `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `FAL_API_KEY`, `ELINA_LORA_URL`, `CLAUDE_MODEL`, `ENV`, `PYTHONPATH`, `PYTHONUNBUFFERED`
- Сеть своя, наружу портов не торчит — классический long-polling-бот.

Этот проект **максимально похож** на TaskFlow Bot по архитектуре (python + Telegram + env-файл + persistent volume под state).

### 2.2 `3x-ui` — панель VPN

- `network_mode: host`, свои бинд-маунты `/opt/vpn/data`, `/opt/vpn/certs`.
- Неинтересен для нас, упомянут для полноты.

### 2.3 `nginx-proxy` — общий reverse-proxy

- Слушает `80` и `8443`.
- Конфиги в `/opt/nginx/conf.d`, SSL в `/opt/nginx/ssl`, static в `/opt/nginx/html`.
- Сидит в `proxy-net` с `external: true` — это **опорная сеть** для всех проектов, которым нужен HTTPS наружу.
- `extra_hosts: host.docker.internal:host-gateway` — проксирует и к контейнерам в `proxy-net`, и к процессам на хосте.

**TaskFlow Bot не нуждается в proxy** (бот только исходящий), так что в `proxy-net` подключать его не надо.

---

## 3. Структура проектов на диске

### Паттерн
- `/opt/<project>` — инфраструктура (nginx, vpn, wireguard).
- `/root/<project>` — приложения (elina).
- `/srv`, `/home` — пусто.

### Найденные docker-compose / Dockerfile

```
/opt/vpn/docker-compose.yml
/opt/nginx/docker-compose.yml
/root/elina/docker-compose.yml
/root/elina/Dockerfile
```

### Посторонние папки в `/opt`

```
/opt/containerd/   — системное
/opt/elina/        — НЕ тот elina (активный проект в /root/elina). Не заглядывал.
/opt/nginx/        — reverse-proxy, описан выше
/opt/project1/     — пустая
/opt/project2/     — пустая
/opt/vpn/          — 3x-ui
/opt/wireguard/    — WireGuard (ls, без подробностей)
```

`project1` и `project2` — видимо заготовки без содержимого (пустые директории), можно считать свободными. Датированы одним днём — похоже на шаблонную раскладку от провайдера.

### Содержимое compose-файлов соседей (для паттерна)

**`/opt/nginx/docker-compose.yml`:**
```yaml
services:
  nginx:
    image: nginx:1.25-alpine
    container_name: nginx-proxy
    restart: unless-stopped
    ports: ["80:80", "8443:8443"]
    volumes:
      - /opt/nginx/conf.d:/etc/nginx/conf.d:ro
      - /opt/nginx/ssl:/etc/nginx/ssl:ro
      - /opt/nginx/html:/usr/share/nginx/html:ro
    extra_hosts: ["host.docker.internal:host-gateway"]
    networks: [proxy-net]
networks:
  proxy-net:
    external: true
```

**`/root/elina/docker-compose.yml`:**
```yaml
services:
  elina:
    build: .
    container_name: elina
    restart: always
    env_file: .env
    environment: [PYTHONPATH=/app]
    volumes:
      - elina_memory:/app/memory/data
      - elina_logs:/app/logs
    networks: [elina_net]
volumes: {elina_memory: {}, elina_logs: {}}
networks: {elina_net: {driver: bridge}}
```

**`/opt/vpn/docker-compose.yml`:** `network_mode: host`, bind-mounts — специфика для VPN, не шаблон.

### `.env` рядом с compose — да
`/root/elina/.env` существует (491 байт, `-rw-r--r--`). **Содержимое не читалось** согласно инструкции. У elina переменные передаются через `env_file: .env`.

`/opt/nginx/` — `.env` нет, всё в compose inline.

---

## 4. Общая инфраструктура (PG / Redis / proxy)

| Тип | Найдено |
|---|---|
| PostgreSQL / postgis / timescale | **НЕТ** ни одного контейнера |
| Redis / valkey / keydb | **НЕТ** |
| Reverse proxy | `nginx-proxy` (описан выше) |

**Принципиальный вывод:** на сервере нет общей БД и общего кэша. У `elina` тоже нет PG/Redis — она хранит состояние в `elina_memory` volume (по всей видимости SQLite / файлы в `/app/memory/data`). Это значит, что шаблон «каждый проект тянет своё хранилище в своём compose» уже установлен.

---

## 5. systemd / автозапуск

Вне Docker и стандартных Ubuntu-сервисов **работают только**:
- `containerd.service`
- `docker.service`

`ls /etc/systemd/system/` — только стандартные симлинки, ничего кастомного. Никаких python-ботов в обход Docker нет.

---

## 6. Ресурсы

### `docker stats --no-stream`
| Контейнер | CPU | MEM |
|---|---|---|
| `elina` | 0.00 % | 141 MiB |
| `3x-ui` | 1.39 % | 79 MiB |
| `nginx-proxy` | 0.00 % | 6 MiB |

Суммарно в Docker: ~230 MiB.

### Память сервера
- `used 802 MiB`, `available 3.0 GiB`, `free 288 MiB`
- Есть `buff/cache 3 GiB`, который система при необходимости вытеснит.
- Swap: 33 MiB использовано из 512.

### Занятые порты (TCP listen)
| Порт | Процесс |
|---|---|
| 22 | sshd |
| 53 | systemd-resolved |
| 80 | docker-proxy → nginx |
| 443 | xray (3x-ui) |
| 2053, 2096 | x-ui (админка/trojan) |
| 8443 | docker-proxy → nginx |
| 62789 (loopback) | xray |

**TaskFlow Bot ни одного порта наружу не требует** (long-polling), конфликтов нет.

---

## 7. Следы предыдущего TaskFlow

Искал по маскам `task|assist|flow|bot` в контейнерах, путях, volumes, сетях:

- Контейнеры: `none`
- Пути в `/opt /srv /root /home`: `none`
- Volumes: `none`
- Сети: `none`

**Старой установки TaskFlow Bot на сервере нет.** Деплоим с чистого листа, ничего не затирается.

---

## 8. Паттерн соседних проектов — сводно

| Аспект | Как сделано |
|---|---|
| Папка проекта | `/opt/<name>/` (инфра) или `/root/<name>/` (приложения) |
| Orchestration | Каждый проект в своём compose. Общего orchestration нет. |
| БД / кэш | **Каждый проект тянет своё.** Общего PG/Redis на хосте нет. `elina` хранит состояние в named volume. |
| Сеть | Своя `bridge`-сеть на проект (`elina_net`). Отдельно есть `proxy-net: external` — в неё лезут только те, кто выставляет HTTP наружу. |
| `.env` | Лежит рядом с `docker-compose.yml`, подключается через `env_file: .env`. Содержимое в VCS не хранится. |
| Имена контейнеров | Явный `container_name`, совпадает с именем проекта (`elina`, `nginx-proxy`, `3x-ui`). |
| Volumes | Named volumes Docker (`elina_memory`, `elina_logs`), без bind-mounts для данных. Bind-mounts только для read-only конфигов (`nginx-proxy`). |
| Restart policy | `always` (elina) и `unless-stopped` (nginx, 3x-ui). Смешанно. |
| Логи | `stdout` контейнера + **отдельный named volume** `*_logs` для файловых логов приложения. `docker logs` доступен всегда. |
| systemd-сервисы | Не заведены. Docker сам поднимается при старте хоста, контейнеры — через restart policy. |

---

## 9. Рекомендация по размещению TaskFlow Bot

### Где
- Папка: **`/root/taskflow_bot`** — следуем паттерну `elina` (приложения в `/root/`, не в `/opt/`). Либо `/opt/taskflow_bot`, если ты хочешь унифицировать все проекты в `/opt/`. **Уточни предпочтение.**
- `docker-compose.yml` и `.env` рядом.

### Имена (чтобы не конфликтовать)
- Контейнер: `taskflow-bot`
- Сеть: `taskflow_net` (bridge, локальная для compose)
- Volumes: `taskflow_data` (для state), `taskflow_logs`

### Как с PG и Redis — 3 варианта, **нужно твоё решение**

| Вариант | Плюсы | Минусы |
|---|---|---|
| **A.** Поднять PG и Redis контейнерами **внутри** нашего compose | Соответствует ТЗ (PostgreSQL), сильнее, чем SQLite, нормальная транзакционная семантика | +1 контейнер PG (~150 MiB), +1 Redis (~10 MiB). Изолированно, но запас памяти на VPS остаётся огромный. |
| **B.** SQLite (named volume) + in-memory cache | Минимум контейнеров (1), совсем лёгкий footprint | Расходится с ТЗ, `pending_results`/`tasks` в файле, рестарт бота очистит только in-memory часть (ключи `awaiting_*`, `stale_notified`). Стейл-уведомление может дубль отправить после рестарта. |
| **C.** Поднять PG и Redis как **общую инфраструктуру хоста** (отдельный compose `/opt/infra/`), подключать будущие проекты к ним | Один PG/Redis на всех, меньше дубль-памяти | Добавляем новую сущность в архитектуру хоста, которой до нас там не было. Решение с последствиями для будущих проектов. |

**Мой default:** **вариант A** (PG + Redis в своём compose). Ресурсов хватает, поведение совпадает с ТЗ, никаких переделок кода бота. Сценарий C стоит обсуждать, только если ты планируешь ещё проекты — сейчас делать его ради одного клиента — преждевременная общность.

### Пример расклада compose (проектирование, не реализация)

```yaml
services:
  bot:
    build: .
    container_name: taskflow-bot
    restart: unless-stopped
    env_file: .env
    depends_on: [postgres, redis]
    volumes: [taskflow_logs:/app/logs]
    networks: [taskflow_net]

  postgres:
    image: postgres:16-alpine
    container_name: taskflow-postgres
    restart: unless-stopped
    env_file: .env      # PG_PASSWORD и пр.
    volumes: [taskflow_pgdata:/var/lib/postgresql/data]
    networks: [taskflow_net]

  redis:
    image: redis:7-alpine
    container_name: taskflow-redis
    restart: unless-stopped
    volumes: [taskflow_redis:/data]
    networks: [taskflow_net]

volumes: { taskflow_logs: {}, taskflow_pgdata: {}, taskflow_redis: {} }
networks: { taskflow_net: { driver: bridge } }
```

`PG_DSN=postgresql://taskflow:...@postgres:5432/taskflow` и `REDIS_URL=redis://redis:6379/0` — по DNS-именам сервисов внутри сети.

---

## Резюме

**Соседние проекты живут так:** каждый — своя папка в `/root/` (приложения) или `/opt/` (инфра), свой `docker-compose.yml` и `.env` рядом, свой bridge-network, named volumes для данных, restart `always`/`unless-stopped`. Общей PG/Redis нет — каждый тянет своё. Общая точка — только `nginx-proxy` в `proxy-net` для публичного HTTP, но нам он не нужен.

**Предлагаю размещать TaskFlow Bot так:** папка `/root/taskflow_bot/`, контейнер `taskflow-bot`, сеть `taskflow_net`, named volumes `taskflow_pgdata / taskflow_redis / taskflow_logs`, рядом — `postgres:16-alpine` и `redis:7-alpine` в том же compose (вариант A). Старой установки нет, конфликтов по именам, портам, volumes, сетям нет.

### Вопросы к тебе перед деплоем
1. **Путь:** `/root/taskflow_bot` (повторяем elina) или `/opt/taskflow_bot` (выравниваем с инфра-проектами)?
2. **PG/Redis:** вариант A (свой PG+Redis в compose, мой default), B (SQLite+in-memory), или C (общий `/opt/infra/` на будущее)?
3. **Доставка кода:** rsync с локальной машины, `git clone` с приватного репо, или я паку архив и `scp` его на сервер? Git-а в проекте нет (ты запрещал коммиты), поэтому без предварительной настройки будет rsync/scp.
4. **SSH-ключ:** ты хочешь, чтобы я на этапе деплоя **один раз** разложил публичный ключ `~/.ssh/id_ed25519.pub` на сервер (через `ssh-copy-id` с паролем), чтобы дальше работать без пароля? Либо оставим пароль на каждую операцию.
5. **Имена секретов в `.env`:** у тебя локально в `.env` есть `BOT_TOKEN`, `AIRTABLE_API_KEY`, `MODERATOR_GROUP_ID`, `MODERATOR_IDS`, `TEST_USER_ID`. На сервере все 1-в-1 или что-то меняется (например, PROD-токен бота другой)?
6. **`TEST_MODE`:** запускать на сервере с `TEST_MODE=false` сразу, или деплоим сначала в TEST_MODE и переключаем после прогона?
