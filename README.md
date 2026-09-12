# TaskFlow Scheduler Bot v2

Telegram-бот для ручной публикации задач из Airtable в Telegram-группу: загрузка, назначение исполнителей, сдача результатов, уведомления о простое.

## 🎯 Обзор

- 📥 **Источник задач:** Airtable (REST API)
- 🗄 **Интеграция:** PostgreSQL (asyncpg), Redis (redis.asyncio)
- 🔄 **Режимы:** TEST_MODE и PRODUCTION
- 📢 **Оповещения:** Telegram (aiogram 3.x)
- 🟢 **Статус:** Рабочий продукт (полный README, продакшн-чеклист, сценарий тестирования)

---

## 📁 Структура

```
taskflow_scheduler_bot/
├── 🚀 run.py              # Точка входа
├── 🔒 .env                # Конфигурация (исключён из Git)
├── 📦 requirements.txt    # Зависимости
├── 📂 docs/               # Документация (см. ниже)
├── app/
│   ├── ⚙️ config.py       # Конфигурация из .env
│   ├── 🗄 db.py           # PostgreSQL (asyncpg)
│   ├── ⚡ cache.py         # Redis
│   ├── 📡 airtable.py     # Airtable REST API
│   ├── ⏰ scheduler.py    # check_stale (1 мин)
│   ├── 🔧 utils.py        # Утилиты
│   ├── ⌨️ keyboards.py    # Inline-клавиатуры
│   └── handlers/
│       ├── 👋 common.py    # /start, /status, /test_*
│       ├── 🛡 moderator.py # Панель, publish, accept/reject
│       ├── 👍 reactions.py # message_reaction
│       └── 📝 executor.py  # ЛС исполнителя, медиа
└── 📊 logs/               # Логи (events.log)
```

---

## 🔐 Безопасность

- 🔒 `.env` исключён из Git — API-ключи через Docker `env_file` или локальный `.env`
- 👥 MODERATOR_IDS — whitelist модераторов по ID
- 🧪 TEST_MODE — изоляция тестовых данных

---

## 🛠 Технологический стек

- 🐍 **Язык:** Python 3.11+
- 🤖 **Telegram:** aiogram 3.x
- 🌐 **Бэкенд:** aiohttp (Airtable), asyncpg (PostgreSQL), redis.asyncio (Redis)
- ⏱ **Планировщик:** APScheduler 3.x (check_stale)
- 🐳 **Инфра:** Docker, docker-compose

---

## 📖 Документация

| 📄 Файл | Назначение |
|---------|-----------|
| `DEPLOY_NOTES.md` | 🚀 Деплой-опера |
| `OPS_GUIDE.md` | 📘 Руководство по эксплуатации |
| `AUDIT_REPORT.md` | 🔍 Аудит безопасности |
| `SCENARIO.md` | 🧪 Сценарий тестирования |
| `TODO.md` | 📋 Бэклог задач |

---

## 📦 Статус

✅ **Рабочий продукт**
- 📥 Загрузка задач из Airtable
- 📤 Публикация по кнопке
- 👤 Назначение исполнителей (реакция в группе)
- ✅ Сдача результатов (Telegram ЛС)
- ⏰ Уведомления о простое (30 мин)
- 🖼 Поддержка медиа (фото/видео/документы)
- 🧪 TEST_MODE для тестирования

❌ **Требуется**
- 📋 Агрегация OPS_GUIDE в README (при необходимости)
- 🔄 Обновление чеклистов

---

*Последнее обновление: 2026-09-13*