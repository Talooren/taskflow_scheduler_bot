# TaskFlow Bot — Аудит реализации

> Дата аудита: 2026-04-17  
> Ревьюер: Claude Code (claude-sonnet-4-6)  
> Аудируемая ветка: рабочая директория `taskflow_scheduler_bot/`  
> Спецификация: `taskflow_v2_testmode_addendum.md` (найден в Downloads); `taskflow_v2_prompt.md` и `taskflow_v2_media_addendum.md` — **не найдены** (gap сам по себе).  
> SCENARIO.md присутствует, но описывает v1-архитектуру (SQLite-only, poll_airtable, published_tasks с retry_count) — расхождение зафиксировано ниже.

---

## Резюме

Реализован работоспособный каркас: PostgreSQL/SQLite dual-backend, Redis/in-memory fallback, панель модератора, accept/reject flow, check_stale scheduler, базовое структурное логирование. Callback-данные хранят `record_id` — бот переживает рестарт.

**Статус запуска:**
- **PROD-режим** — **запуск возможен** при условии устранения п.6 (filter crash) и понимания ограничений (нет retry Airtable, нет TTL-бэкапа результатов).
- **TEST_MODE** — **запуск заблокирован**: ключевой сценарий (загрузить → опубликовать → взять) сломан двумя независимыми багами. Нужна ещё одна итерация правок.

**Итого:**
- Критичных проблем: **3** (блокируют TEST_MODE)
- Средних проблем: **7** (не блокируют PROD, но создают риски)
- Мелких замечаний: **7**

---

## Критичные проблемы (блокируют запуск TEST_MODE)

### КР-1: TEST_MODE — ключевой сценарий полностью сломан

**Файл:** `app/handlers/moderator.py:159`

В `_handle_task_count_input` при отображении загруженных задач:

```python
kb = test_take_task_kb(task["record_id"]) if cfg.test_mode else publish_task_kb(task["record_id"])
```

В TEST_MODE на загруженных задачах показывается кнопка **«Взять задачу (тест)»** вместо **«Опубликовать»**. При нажатии вызывается `on_take_test` → `_assign_user` → `db.update_task_assigned(record_id)`, который содержит `WHERE status = 'published'`. Задача имеет статус `loaded` → `UPDATE 0` → функция молча возвращает None. Но `callback.answer("Задача взята!")` всё равно срабатывает — пользователь видит ложное подтверждение.

Последствие: в TEST_MODE задача никогда не переходит в статус `published`, `_assign_user` всегда проваливается, pending_results остаётся пустым, весь сценарий тестирования (пункты 3–8 из spec-addendum) не работает.

Корень: в `_handle_task_count_input` нужно **всегда** показывать `publish_task_kb` (включая TEST_MODE). Хэндлер `on_publish` уже правильно обрабатывает TEST_MODE: публикует в `moderator_group_id` с кнопкой `test_take_task_kb`. Условие `cfg.test_mode` в строке 159 нужно убрать.

---

### КР-2: `db.get_pool()` не существует — три команды TEST_MODE падают с AttributeError

**Файлы:**
- `app/handlers/common.py:14` — функция `_con_fetch`
- `app/handlers/common.py:64` — `cmd_test_status`
- `app/handlers/common.py:106` — дублирующая функция `con_fetch`
- `app/scheduler.py:55` — `check_stale_force`

Все четыре места вызывают `db.get_pool()`, которой не существует в `app/db.py` (там есть только module-level переменная `_pg_pool`). Результат:

- `/test_status` → `AttributeError: module 'app.db' has no attribute 'get_pool'`
- `/test_reset` → то же через `_con_fetch`
- `/test_skip_stale` → вызывает `check_stale_force()`, которая сразу падает

Все три тест-команды нерабочие. Нужно либо добавить в `db.py` функцию `get_pool()`, возвращающую `_pg_pool`, либо переписать обращения к БД через уже существующие функции `db.*` вместо прямого доступа к пулу.

Дополнительно: `check_stale_force` в SQLite-режиме тоже падает, потому что напрямую обращается к asyncpg pool (`pool.acquire()`), тогда как при `_use_postgres = False` пул не инициализируется. Нужно унифицировать с `db.get_stale_published()`, добавив параметр для игнорирования порога.

Дополнительно к КР-2: в `common.py` есть две идентичные функции: `_con_fetch` (строки 12–18) и `con_fetch` (строки 105–110). Одну нужно удалить.

---

### КР-3: Повторная публикация (`restale_`) в TEST_MODE без кнопки взятия

**Файл:** `app/handlers/moderator.py:397–403`

В `on_restale` при переопубликации задачи сообщение отправляется без `test_take_task_kb`:

```python
msg = await bot.send_message(chat_id, text, parse_mode="MarkdownV2")
```

В TEST_MODE `chat_id = cfg.target_chat_id() = moderator_group_id`. Сообщение приходит без кнопки «Взять задачу (тест)». Модератор видит задачу, но не может её взять — кнопка `on_publish` уже не показывается (она была на прежнем сообщении, которое удалили). Задача зависает.

В `on_restale` нужно добавить ту же ветку, что и в `on_publish`: если `cfg.test_mode` — добавить `reply_markup=test_take_task_kb(record_id)`.

---

## Средние проблемы (не блокируют PROD, но нужно исправить)

### СР-1: Нет retry для Airtable PATCH — возможна потеря данных

**Файл:** `app/airtable.py:95–107`

`patch_record` перехватывает `Exception` и возвращает `None`. При сетевом таймауте HTTP-запрос может успеть дойти до Airtable (HTTP 200 уже в пути), но ответ потерян. Повтора нет. Проверки текущего статуса записи в Airtable перед PATCH нет.

Практический сценарий: `on_accept` → `airtable.set_result_done()` → timeout → возвращает None → PostgreSQL помечен `done`, Redis `result_text` удалён, а в Airtable статус остался «В работе». Расхождение данных незаметно.

Решение: добавить retry с экспоненциальной задержкой (2-3 попытки). При финальной неудаче — логировать ERROR + уведомить в `moderator_group_id`. Предварительная проверка статуса Airtable перед PATCH — желательна, но не критична (idempotent-запись одинаковых значений не вредит).

---

### СР-2: Результат пропадает через 24 часа — нет бэкапа в PostgreSQL

**Файл:** `app/handlers/executor.py:46`, `app/cache.py:161`

`set_result_text(record_id, result_content, ttl=86400)` — единственное место хранения результата. В PostgreSQL результат не дублируется. Если модератор не нажал «Принять» за 24 часа:

- `cache.get_result_text(record_id)` вернёт `None`
- `on_accept` вызовет `airtable.set_result_done(record_id, "", end_time, mode)` с пустым результатом
- В Airtable запишется пустая строка, оригинальное сообщение исполнителя потеряно

Решение: сохранять `result_content` в отдельное поле `result_text TEXT` в таблице `pending_results` как первичное хранилище, Redis использовать как кэш. При `on_accept` читать из PostgreSQL, если Redis пуст.

---

### СР-3: Нет напоминаний модератору о непринятых результатах

Требование из `taskflow_v2_testmode_addendum.md` (косвенно, через упоминание 24h TTL): напоминание через 4ч / 12ч / 24ч после получения результата. **Не реализовано.** `check_stale` следит только за задачами без исполнителя, а не за задачами, ожидающими модерации.

Решение: добавить в scheduler отдельный джоб `check_pending_approvals`, который читает из PostgreSQL все записи в `pending_results` старше N часов и отправляет напоминание в `moderator_group_id`. Для хранения времени напоминания использовать `assigned_at` + фиксированные дельты (4h, 12h, 24h).

---

### СР-4: Токен бота в URL медиафайлов — нет предупреждения в логах

**Файл:** `app/handlers/executor.py:109`

```python
file_url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
```

Ссылка содержит полный токен бота и имеет ограниченное время жизни (Telegram удаляет файлы через ~24–48 часов). В логах нет никакого предупреждения. Запись этой ссылки в Airtable создаёт ложное ощущение постоянного хранения.

Решение: добавить `logger.warning("Media URL contains bot token and has limited TTL (Telegram deletes files). record_id=%s", record_id)` сразу после формирования `file_url`.

---

### СР-5: `publishing_enabled` не имеет UI и нигде не проверяется

**Файл:** `app/db.py:658–667`

Функции `is_publishing_enabled()` и `set_publishing()` определены, в БД создаётся таблица `settings`, но:
1. Нигде не вызываются (`grep` по всей кодовой базе — 0 вызовов вне db.py).
2. В панели модератора нет кнопки переключения.
3. `check_stale` и `on_publish` не проверяют этот флаг.

Пользователь не может паузить/возобновлять публикацию через UI, как описано в SCENARIO.md.

Решение: добавить в `moderator_panel_kb()` кнопки «Включить/Остановить публикацию» и проверять `is_publishing_enabled()` в `on_publish`. Кнопку `clear_queue` оставить — она не эквивалентна паузе.

---

### СР-6: Фильтр хэндлера может упасть с AttributeError

**Файл:** `app/handlers/moderator.py:94`

```python
@router.message(lambda m: not m.text.startswith("/"))
```

Если `m.text is None` (медиасообщение, форвард, стикер) — `AttributeError: 'NoneType' object has no attribute 'startswith'`. Особенно актуально, если модераторская группа использует медиа.

Решение: заменить на `lambda m: not (m.text or "").startswith("/")`.

---

### СР-7: Суперgruппа — `message_reaction` не приходит, механизм реакций молча не работает

Нигде в коде, README или docstrings не задокументировано: в супергруппах с большим числом участников Telegram API отправляет `message_reaction_count` (агрегированный, без `user_id`), а не `message_reaction`. Хэндлер `on_reaction` зарегистрирован только на `message_reaction` (`@router.message_reaction()`), который в больших супергруппах не приходит вовсе. Проверка `if not event.user: return` в `on_reaction` не помогает — до неё даже не доходит.

Это означает, что механизм «первый поставил реакцию — берёт задачу» в реальной рабочей группе с >100 участников **может никогда не сработать**.

Решение: добавить в README и в `on_reaction` явный комментарий-предупреждение. Рекомендовать держать группу исполнителей малой (≤50 человек) или рассмотреть альтернативный механизм взятия (команда `/take <record_id>`).

---

## Мелкие замечания и улучшения

### МЗ-1: Нет глобального error handler в aiogram

**Файл:** `run.py`

Dispatcher не имеет зарегистрированного `errors_handler`. Необработанные исключения в хэндлерах aiogram логируются самим фреймворком, но не отправляются в `moderator_group_id`. Невидимые сбои.

Решение: добавить `@dp.errors()` хэндлер с `logger.error(...)` и опциональным `bot.send_message(cfg.moderator_group_id, ...)` для критических ошибок.

---

### МЗ-2: APScheduler не уведомляет при падении джоба

**Файл:** `app/scheduler.py`

APScheduler по умолчанию логирует неперехваченные исключения в джобах на уровне WARNING, но не уведомляет модераторов. Если `check_stale` упадёт (например, из-за недоступности PostgreSQL), уведомления о простое перестанут работать незаметно.

Решение: подписаться на `EVENT_JOB_ERROR` через `scheduler.add_listener(on_job_error, EVENT_JOB_ERROR)` и отправлять уведомление в `moderator_group_id`.

---

### МЗ-3: Гонка при одновременном взятии двух задач одним пользователем

**Файл:** `app/handlers/moderator.py:442–458`

Теоретическая TOCTOU-гонка: пользователь A реагирует на задачу Z и задачу W в пределах одного event-loop цикла. Оба `on_reaction` → `_assign_user` проходят `has_pending_result(A)` = False до того, как любой из них завершит `insert_pending_result`. Оба `update_task_assigned` могут вернуть True (Z и W — разные задачи). `insert_pending_result` использует `ON CONFLICT (user_id) DO UPDATE`, то есть второй вызов перезапишет первый. Итог: задача Z останется в статусе `assigned` с записью в `pending_results`, указывающей на W.

В PostgreSQL одновременные реакции на два разных сообщения в пределах одного цикла крайне маловероятны. Тем не менее:

Решение (минимальное): изменить `ON CONFLICT (user_id) DO UPDATE` на `ON CONFLICT (user_id) DO NOTHING` в `insert_pending_result`; добавить `UNIQUE (record_id)` на таблицу `pending_results` как belt-and-suspenders — чтобы дублирующие назначения на одну задачу были невозможны на уровне БД.

---

### МЗ-4: `/test_status` не показывает `publishing_enabled`

**Файл:** `app/handlers/common.py:80–84`

По спецификации команда должна выводить:
```
⚙️ Настройки:
   publishing_enabled: да/нет
```

В реализации выводится только `TEST_MODE` и `TEST_USER_ID`. `publishing_enabled` не отображается.

---

### МЗ-5: Дублирующиеся функции `_con_fetch` / `con_fetch`

**Файл:** `app/handlers/common.py:12–18` и `105–110`

Две идентичные функции с разными именами. Обе ссылаются на `db.get_pool()` (несуществующий метод). Удалить одну.

---

### МЗ-6: Логика rate limiting Airtable работает некорректно

**Файл:** `app/airtable.py:39–43`

```python
async def _rate_limit():
    limiter = _get_rate_limiter()
    async with limiter:
        await asyncio.sleep(0.2)
```

`asyncio.sleep(0.2)` внутри `asyncio.Semaphore(5)` означает, что 5 одновременных запросов будут спать по 0.2 с каждый, держа семафор занятым. Реальное ограничение составляет ~25 req/s (не 5 как можно подумать). Формально не критично (Airtable позволяет 5 req/s на ключ), но текущая реализация может превысить лимит при пиковой нагрузке.

Решение: семафор убрать, заменить на честный rate limiter (например, `asyncio.sleep(0.22)` между запросами в очереди через отдельный asyncio.Lock).

---

### МЗ-7: SCENARIO.md описывает устаревшую v1-архитектуру

**Файл:** `SCENARIO.md`

Документ описывает:
- Таблицу `published_tasks` (в коде — `tasks`)
- Поле `retry_count` (в коде — отсутствует)
- Джоб `poll_airtable` каждые 5 минут (в коде — отсутствует)
- Порог simple 15 минут (в коде — 30 минут)
- TEST_MODE как авто-назначение в ЛС (в коде — кнопка в группе)

SCENARIO.md создаёт путаницу для разработчика. Нужно либо обновить под v2, либо удалить и сослаться на README.

---

## Что реализовано корректно

- **Гонка при взятии задачи (двумя разными пользователями)**: `update_task_assigned` с `WHERE status = 'published'` — атомарный UPDATE в PostgreSQL корректно разрешает конфликт. Только один пользователь получит `UPDATE 1`. КР-1 выше относится к другой проблеме (TEST_MODE), а не к этой гонке.
- **`allowed_updates` включает `message_reaction`**: `run.py:71–76` — правильно.
- **Callback_data хранит `record_id` и `user_id`**: `keyboards.py:28–34` — бот переживает рестарт без потери состояния кнопок.
- **Идемпотентная вставка задач**: `insert_task` использует `ON CONFLICT (record_id) DO NOTHING` — повторные вызовы безопасны.
- **TEST_MODE: TEST_USER_ID добавляется в moderator_ids**: `config.py:60–61` — верно.
- **`cfg.target_chat_id()`**: корректно возвращает `moderator_group_id` в TEST_MODE.
- **`on_publish` в TEST_MODE**: правильно публикует в `moderator_group_id` с кнопкой `test_take_task_kb` и `[🧪 TEST]` префиксом.
- **`on_restale` сбрасывает stale-ключ**: `cache.del_stale_notified(record_id)` вызывается в `on_restale` (moderator.py:406). Ключ корректно сбрасывается при переопубликации — новая публикация получит свежий 30-минутный отсчёт.
- **Семантика «один исполнитель — одна задача»**: `has_pending_result(user_id)` в `_assign_user` + UNIQUE(user_id) в pending_results. «Одна задача — один исполнитель»: `update_task_assigned` WHERE status='published'. Обе стороны реализованы.
- **PostgreSQL / SQLite dual-backend**: корректная инициализация и fallback.
- **Redis / in-memory dual-backend**: корректная инициализация и fallback.
- **Базовое структурное логирование**: INFO/WARNING/ERROR, ротируемый файл `logs/bot.log`.
- **Переключение awaiting_task_count → awaiting_reject_reason через Redis**: корректный FSM-паттерн без aiogram FSM.
- **Команды `/test_status`, `/test_reset`, `/test_skip_stale` ограничены `if not cfg.test_mode: return`**: изоляция соблюдена.
- **TEST_MODE не смешивается хаотично**: основных точек с `cfg.test_mode` — 5-6, все осмысленные.

---

## Расхождения со спецификацией

| # | Спецификация (taskflow_v2_testmode_addendum.md) | Реализация |
|---|---|---|
| 1 | После загрузки задач показывать `[✅ Опубликовать]` даже в TEST_MODE | Показывает `[👤 Взять задачу (тест)]` вместо `[✅ Опубликовать]` (КР-1) |
| 2 | `/test_status` показывает `publishing_enabled: да/нет` | Не показывает (МЗ-4) |
| 3 | `/test_reset`: `DELETE FROM pending_results WHERE username = TEST_USER_ID username` | Удаляет ВСЕ pending_results, не только тестовые |
| 4 | `/test_skip_stale`: игнорировать 30-минутный порог | Реализовано в `check_stale_force`, но функция падает (КР-2) |
| 5 | SCENARIO.md: таблица `published_tasks` с `retry_count` | Реализована таблица `tasks` без `retry_count` (устаревший документ) |
| 6 | SCENARIO.md: TEST_MODE = авто-назначение TEST_USER_ID в ЛС | Реализован как кнопка в группе модераторов (правильный подход per addendum, SCENARIO.md устарел) |
| 7 | Напоминания модератору через 4ч/12ч/24ч | Не реализовано (СР-3) |
| 8 | `publishing_enabled` с UI в `/панель` | Функции есть в DB, UI нет, нигде не проверяется (СР-5) |

---

## Рекомендуемый порядок правок (следующая итерация)

**Блок А — TEST_MODE (запуск тестирования)**

1. **КР-1**: `moderator.py:159` — убрать условие `cfg.test_mode`, всегда показывать `publish_task_kb` для загруженных задач.
2. **КР-2**: добавить `get_pool()` в `db.py` (возвращает `_pg_pool`, или кидает RuntimeError если SQLite). Переписать `check_stale_force` через `db.get_stale_published(0)` или новый параметр.
3. **КР-3**: `moderator.py:397` — в `on_restale` добавить `reply_markup=test_take_task_kb(record_id)` при `cfg.test_mode`.
4. **СР-6**: `moderator.py:94` — исправить filter: `lambda m: not (m.text or "").startswith("/")`.

**Блок Б — Надёжность данных (PROD)**

5. **СР-2**: добавить поле `result_text TEXT` в `pending_results`, сохранять при получении результата. В `on_accept` читать из PostgreSQL, если Redis пуст.
6. **СР-1**: добавить retry (3 попытки, exponential backoff) в `patch_record`. Уведомление в moderator_group при финальной ошибке.
7. **СР-4**: добавить `logger.warning("Bot token in media URL...")` в `executor.py:109`.

**Блок В — Функциональность по спецификации**

8. **СР-5**: добавить кнопки «Включить/Остановить публикацию» в `moderator_panel_kb()`, проверять `is_publishing_enabled()` в `on_publish`, `check_stale`.
9. **СР-3**: добавить джоб `check_pending_approvals` в `scheduler.py` — напоминания через 4ч/12ч/24ч.
10. **МЗ-4**: вывести `publishing_enabled` в `/test_status`.

**Блок Г — Мониторинг и документация**

11. **МЗ-1**: добавить `@dp.errors()` handler в `run.py`.
12. **МЗ-2**: подписаться на `EVENT_JOB_ERROR` в APScheduler.
13. **СР-7**: добавить в README предупреждение о `message_reaction_count` в супергруппах.
14. **МЗ-5**: удалить дублирующую функцию `con_fetch` в `common.py`.
15. **МЗ-7**: обновить или удалить SCENARIO.md (устаревшая v1 документация).
16. **МЗ-3**: изменить `ON CONFLICT (user_id) DO UPDATE` → `DO NOTHING` в `insert_pending_result`; рассмотреть добавление `UNIQUE (record_id)` в `pending_results`.

---

*Файл сгенерирован аудитом кода без внесения правок. Для следующей итерации — реализовывать блоки А и Б в первую очередь.*
