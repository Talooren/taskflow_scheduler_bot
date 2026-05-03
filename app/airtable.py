"""
Асинхронный клиент Airtable REST API с rate limiting.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import aiohttp

from app.config import cfg

logger = logging.getLogger(__name__)

_BASE = "https://api.airtable.com/v0"
_rate_limiter: asyncio.Semaphore | None = None


def _get_rate_limiter() -> asyncio.Semaphore:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = asyncio.Semaphore(5)
    return _rate_limiter


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg.airtable_api_key}",
        "Content-Type": "application/json",
    }


def _table_url(suffix: str = "") -> str:
    table = quote(cfg.airtable_table, safe="")
    return f"{_BASE}/{cfg.airtable_base_id}/{table}{suffix}"


async def _rate_limit():
    limiter = _get_rate_limiter()
    async with limiter:
        await asyncio.sleep(0.2)


async def _handle(resp: aiohttp.ClientResponse) -> dict | None:
    if resp.status == 429:
        logger.warning("Airtable rate-limit (429) — ожидание 30 сек")
        await asyncio.sleep(30)
        return None
    if resp.status == 401:
        logger.error("Airtable auth error (401) — проверьте AIRTABLE_API_KEY")
        return None
    if resp.status >= 400:
        body = await resp.text()
        logger.error("Airtable error %s: %s", resp.status, body)
        return None
    if resp.status in (200, 201):
        return await resp.json()
    return None


_PATCH_RETRY_STATUSES = {429, 500, 502, 503, 504}
_PATCH_RETRY_ATTEMPTS = 3
_PATCH_RETRY_BACKOFF = (1, 2, 4)


async def _patch_once(record_id: str, fields: dict) -> tuple[int, dict | None, str | None]:
    """Одна попытка PATCH. Возвращает (status, data|None, retry_after|None)."""
    await _rate_limit()
    async with aiohttp.ClientSession() as session:
        async with session.patch(
            _table_url(f"/{record_id}"),
            headers=_headers(),
            json={"fields": fields},
        ) as resp:
            retry_after = resp.headers.get("Retry-After")
            if resp.status in (200, 201):
                return resp.status, await resp.json(), retry_after
            return resp.status, None, retry_after


async def fetch_queue_tasks(page_size: int = 50) -> list[dict]:
    """Получить задачи со статусом 'Очередь'."""
    formula = '{Статус}="Очередь"'
    params = {
        "filterByFormula": formula,
        "sort[0][field]": "Дата публикации",
        "sort[0][direction]": "asc",
        "pageSize": page_size,
    }
    await _rate_limit()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(_table_url(), headers=_headers(), params=params) as resp:
                data = await _handle(resp)
                return (data or {}).get("records", [])
        except Exception as e:
            logger.error("Airtable fetch error: %s", e)
            return []


async def fetch_record(record_id: str) -> dict | None:
    await _rate_limit()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                _table_url(f"/{record_id}"), headers=_headers()
            ) as resp:
                return await _handle(resp)
        except Exception as e:
            logger.error("Airtable fetch record %s error: %s", record_id, e)
            return None


async def record_exists(record_id: str) -> bool | None:
    """Проверить, существует ли запись в Airtable.

    Возвращает:
      True  — запись существует (HTTP 200);
      False — запись точно удалена (HTTP 404 NOT_FOUND);
      None  — не удалось определить (сеть / 401 / 429 / 5xx). Используется
              шедулером sync_with_airtable: при None задача НЕ отменяется,
              чтобы транзиентная ошибка Airtable не превратилась в массовую
              отмену задач.
    """
    await _rate_limit()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                _table_url(f"/{record_id}"), headers=_headers()
            ) as resp:
                if resp.status in (200, 201):
                    return True
                if resp.status == 404:
                    return False
                # Прочитываем тело, чтобы Airtable отличил 404 NOT_FOUND_FOR_RECORD
                # от 404 базы или прочих ошибок. Но в любом случае считаем
                # «не уверены» и оставляем задачу как есть.
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("record_exists(%s) network error: %s", record_id, e)
            return None


async def patch_record(record_id: str, fields: dict) -> dict | None:
    """PATCH с идемпотентной проверкой и ретраем (3 попытки, 1/2/4 с).

    Ретраит на aiohttp.ClientError / asyncio.TimeoutError и на HTTP
    429/500/502/503/504. На 429 уважает Retry-After, если сервер его прислал.
    Перед попыткой пишет «Завершено» проверяет, что запись ещё не закрыта —
    защищает от двойной записи результата при ретрае после таймаута.
    """
    # Идемпотентность: если собираемся перевести задачу в «Завершено»,
    # убедимся, что она ещё не закрыта — чтобы не затереть чужой результат
    # при повторе запроса после таймаута.
    target_status = fields.get("Статус")
    if target_status == "Завершено":
        current = await fetch_record(record_id)
        if current:
            cur_status = (current.get("fields") or {}).get("Статус")
            if cur_status == "Завершено":
                logger.warning(
                    "Airtable: %s уже 'Завершено' — пропускаю повторную запись результата",
                    record_id,
                )
                return current

    last_error: str | None = None
    for attempt in range(_PATCH_RETRY_ATTEMPTS):
        try:
            status, data, retry_after = await _patch_once(record_id, fields)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_error = f"network: {e!r}"
            logger.warning(
                "Airtable patch %s попытка %d/%d сетевая ошибка: %s",
                record_id, attempt + 1, _PATCH_RETRY_ATTEMPTS, e,
            )
        else:
            if data is not None:
                return data
            if status not in _PATCH_RETRY_STATUSES:
                # 4xx клиентская ошибка, которую повтор не починит —
                # и 401, и «неизвестное поле» и т.п.
                logger.error("Airtable patch %s HTTP %s — не ретраим", record_id, status)
                return None
            last_error = f"HTTP {status}"
            if status == 429 and retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = _PATCH_RETRY_BACKOFF[attempt]
                logger.warning(
                    "Airtable patch %s 429, Retry-After=%s — ждём %.1fс",
                    record_id, retry_after, delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.warning(
                "Airtable patch %s попытка %d/%d HTTP %s",
                record_id, attempt + 1, _PATCH_RETRY_ATTEMPTS, status,
            )

        if attempt < _PATCH_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(_PATCH_RETRY_BACKOFF[attempt])

    logger.error(
        "Airtable patch %s провалился после %d попыток: %s",
        record_id, _PATCH_RETRY_ATTEMPTS, last_error,
    )
    return None


# ── Удобные обёртки ────────────────────────────────────────────────────────────

async def _lookup_record_id_by_telegram(table: str, username: str) -> str | None:
    """Найти record_id в таблице по полю `Телеграм` (case-insensitive).

    Толерантен к обоим форматам: ищет и `handle`, и `@handle`, т.к. в
    одних таблицах пишут с `@`, в других — без. Возвращает None если не
    найден — вызывающий код должен принять это как «не к чему привязывать»
    и просто не писать соответствующее поле.
    """
    if not username:
        return None
    bare = username.lstrip("@")
    with_at = f"@{bare}"
    # filterByFormula: матчит оба варианта написания, регистронезависимо
    formula = (
        f"OR(LOWER({{Телеграм}})=LOWER('{bare}'),"
        f"LOWER({{Телеграм}})=LOWER('{with_at}'))"
    )
    await _rate_limit()
    async with aiohttp.ClientSession() as session:
        try:
            table_url = f"{_BASE}/{cfg.airtable_base_id}/{quote(table, safe='')}"
            async with session.get(
                table_url,
                headers=_headers(),
                params={"filterByFormula": formula, "maxRecords": "1"},
            ) as resp:
                data = await _handle(resp)
                records = (data or {}).get("records", [])
                if records:
                    return records[0].get("id")
        except Exception as e:
            logger.warning("Airtable lookup %s?Телеграм=%s failed: %s", table, with_at, e)
    logger.info("Airtable lookup: %s с Телеграм=%s/%s не найден", table, bare, with_at)
    return None


async def fetch_all_team_telegrams() -> set[str]:
    """Вернуть множество нормализованных (lowercase, без `@`) значений поля
    «Телеграм» из всех записей таблицы «Команда». Используется для
    наполнения cfg.moderator_usernames на старте бота и в периодическом
    обновлении кэша (каждые 5 мин). Пагинация учтена — берём все страницы.
    """
    result: set[str] = set()
    offset: str | None = None
    async with aiohttp.ClientSession() as session:
        while True:
            await _rate_limit()
            params = {"pageSize": "100"}
            if offset:
                params["offset"] = offset
            try:
                async with session.get(
                    _BASE + f"/{cfg.airtable_base_id}/{quote('Команда', safe='')}",
                    headers=_headers(),
                    params=params,
                ) as resp:
                    data = await _handle(resp)
                    if not data:
                        break
                    for rec in data.get("records", []):
                        tg = rec.get("fields", {}).get("Телеграм")
                        if isinstance(tg, str) and tg.strip():
                            result.add(tg.strip().lstrip("@").lower())
                    offset = data.get("offset")
                    if not offset:
                        break
            except Exception as e:
                logger.warning("fetch_all_team_telegrams failed: %s", e)
                break
    return result


async def get_assistant_record_id(username: str | None) -> str | None:
    """Публичная обёртка: record_id исполнителя по полю Телеграм в таблице
    «Исполнители». Используется в on_reaction (проверка права на взятие) и
    в /start (детект роли). None — если username пустой или не найден."""
    return await _lookup_record_id_by_telegram("Исполнители", username) if username else None


async def get_team_record_id(username: str | None) -> str | None:
    """Публичная обёртка: record_id члена команды по Телеграм в таблице «Команда»."""
    return await _lookup_record_id_by_telegram("Команда", username) if username else None


async def set_published(
    record_id: str,
    publish_time_utc: str,
    group_name: str,
) -> bool:
    """Перевести задачу в «Опубликована»: пишет «Статус», «Дата публикации»
    (фактический момент публикации) и «Группа» (singleSelect, имя из cfg).

    Группа жёстко берётся из cfg.publish_group_name — даже если бот сейчас
    шлёт в тестовый чат, в Airtable фиксируется реальное имя группы.
    """
    fields = {
        "Статус": "Опубликована",
        "Дата публикации": publish_time_utc,
        "Группа": group_name,
    }
    result = await patch_record(record_id, fields)
    if result:
        logger.info("Airtable: %s -> Опубликована (Группа=%s)", record_id, group_name)
    return result is not None


async def set_status_queue(record_id: str) -> bool:
    """Откатить задачу обратно в «Очередь»: сбрасывает «Статус» и затирает
    «Исполнитель», «Время начала», «Время окончания». Используется при
    отмене задачи модератором (кнопка «Отменить») и при обнаружении ручного
    удаления записи в Airtable шедулером — но в случае удаления вызов уже
    бессмысленен (записи нет), поэтому он только для отмены.
    """
    fields: dict = {
        "Статус": "Очередь",
        "Исполнитель": [],
        "Время начала": None,
        "Время окончания": None,
    }
    result = await patch_record(record_id, fields)
    if result:
        logger.info("Airtable: %s -> Очередь (отмена)", record_id)
    return result is not None


async def set_assignee(record_id: str, username: str, start_time_utc: str) -> bool:
    fields: dict = {
        "Статус": "В работе",
        "Время начала": start_time_utc,
    }
    # Ищем запись исполнителя в таблице «Исполнители» по Телеграм
    assignee_id = await _lookup_record_id_by_telegram("Исполнители", username)
    if assignee_id:
        fields["Исполнитель"] = [assignee_id]
    else:
        logger.warning(
            "set_assignee(%s): исполнитель @%s не найден в таблице «Исполнители», "
            "поле оставлено пустым",
            record_id, username,
        )
    result = await patch_record(record_id, fields)
    if result:
        logger.info(
            "Airtable: %s -> В работе, исполнитель=@%s (rec=%s)",
            record_id, username, assignee_id,
        )
    return result is not None


async def set_result_done(
    record_id: str,
    result_text: str,
    end_time_utc: str,
    mode: str | None,
    moderator_username: str | None = None,
) -> bool:
    fields: dict = {
        "Статус": "Завершено",
        "Время окончания": end_time_utc,
    }
    if mode == "Проверка":
        fields["Результат проверки"] = result_text
    else:
        fields["Результат"] = result_text

    # Поле «Модератор» в таблице «Итерация» ссылается именно на таблицу «Команда»
    # (multipleRecordLinks → Команда). Если положить record_id из другой таблицы,
    # Airtable вернёт 422. Поэтому ищем только там.
    if moderator_username:
        mod_id = await _lookup_record_id_by_telegram("Команда", moderator_username)
        if mod_id:
            fields["Модератор"] = [mod_id]
        else:
            logger.warning(
                "set_result_done(%s): модератор @%s не найден в «Команда», "
                "поле Модератор не будет записано. Заведите пользователя в таблице «Команда».",
                record_id, moderator_username,
            )

    result = await patch_record(record_id, fields)
    if result:
        logger.info("Airtable: %s -> Завершено", record_id)
    return result is not None


async def get_record_mode(record_id: str) -> str | None:
    record = await fetch_record(record_id)
    if record:
        fields = record.get("fields", {})
        return fields.get("Режим")
    return None


# ── Вопросы (таблица «Вопросы») ────────────────────────────────────────────────

_QUESTIONS_TABLE = "Вопросы"


async def create_question(
    iteration_record_id: str,
    executor_username: str,
    text: str,
    blocking: bool,
) -> str | None:
    """Создаёт запись в таблице «Вопросы» Airtable.

    Поле Задача в «Вопросы» ссылается на таблицу «Задачи», а не на
    «Итерация». Поэтому сначала тянем Iteration-запись, оттуда читаем
    multipleRecordLinks «Задача» и используем именно task_id как линк.
    Возвращает record_id созданного вопроса (для последующего ответа)
    или None при любой ошибке.
    """
    # 1. Получить task_id из Iteration.Задача
    iteration = await fetch_record(iteration_record_id)
    if not iteration:
        logger.warning("create_question(%s): итерация не найдена", iteration_record_id)
        return None
    task_links = (iteration.get("fields") or {}).get("Задача")
    task_id = task_links[0] if isinstance(task_links, list) and task_links else None

    # 2. Найти исполнителя в «Исполнители»
    executor_id = await _lookup_record_id_by_telegram("Исполнители", executor_username) \
        if executor_username else None

    # 3. POST в «Вопросы»
    fields: dict = {
        "Текст": text,
        "Блокирующий": bool(blocking),
    }
    if task_id:
        fields["Задача"] = [task_id]
    else:
        logger.warning(
            "create_question(%s): у итерации не заполнено поле Задача — "
            "вопрос создаётся без линка на задачу",
            iteration_record_id,
        )
    if executor_id:
        fields["Исполнитель"] = [executor_id]

    table_url = f"{_BASE}/{cfg.airtable_base_id}/{quote(_QUESTIONS_TABLE, safe='')}"
    await _rate_limit()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                table_url, headers=_headers(), json={"fields": fields},
            ) as resp:
                data = await _handle(resp)
                if data and isinstance(data, dict):
                    qid = data.get("id")
                    logger.info(
                        "Airtable: создан вопрос %s (Задача=%s, Исполнитель=%s, Блокирующий=%s)",
                        qid, task_id, executor_id, blocking,
                    )
                    return qid
    except Exception as e:
        logger.error("create_question failed: %s", e)
    return None


async def create_review_iteration(
    parent_record_id: str,
    excluded_username: str | None,
) -> dict | None:
    """Создаёт в «Итерация» новую запись Режим=Проверка по родительской
    Выполнение-итерации parent_record_id.

    Тянет с парента: Задача (линк), Группа, Название задачи, Текст задачи.
    Заполняет:
      - Задача = parent.Задача (тот же линк)
      - Режим = "Проверка"
      - Тип проверки = "Проверка ассистентом"
      - Группа = parent.Группа
      - Лимит (час) = REVIEW_TIME_LIMIT_MIN / 60
      - Для отправки = шаблон карточки (Q4: берём Задача.Текст задачи через lookup)
      - Статус = "Очередь"

    Возвращает dict с ключами:
      - record_id: Airtable record_id новой записи
      - group: имя группы (для последующего sync — куда публикуется)
      - task_name: Название задачи (для локальной БД)
      - task_text: текст для отправки (для локальной БД)
      - excluded_username: оригинальный исполнитель (для tasks.excluded_username)
      - parent_record_id: родительская итерация (для tasks.parent_record_id)
    Или None при ошибке.
    """
    parent = await fetch_record(parent_record_id)
    if not parent:
        logger.warning("create_review_iteration: parent %s не найден", parent_record_id)
        return None

    pf = parent.get("fields", {})
    task_links = pf.get("Задача") or []
    if not isinstance(task_links, list) or not task_links:
        logger.warning(
            "create_review_iteration(%s): у родителя пусто поле Задача — нечего ревьюить",
            parent_record_id,
        )
        return None

    # Lookup-поля «Название задачи»/«Текст задачи» возвращаются как list
    name_lu = pf.get("Название задачи")
    if isinstance(name_lu, list) and name_lu:
        task_name = str(name_lu[0]).strip()
    elif isinstance(name_lu, str):
        task_name = name_lu.strip()
    else:
        task_name = ""

    text_lu = pf.get("Текст задачи")
    if isinstance(text_lu, list) and text_lu:
        task_text_orig = str(text_lu[0])
    elif isinstance(text_lu, str):
        task_text_orig = text_lu
    else:
        task_text_orig = pf.get("Для отправки", "")

    group = pf.get("Группа") or cfg.publish_group_name
    limit_hours = round(cfg.review_time_limit_min / 60.0, 4)

    excl = excluded_username.lstrip("@") if excluded_username else ""
    excl_line = f"Не может взять: @{excl}" if excl else ""

    review_text = (
        f"Проверка задачи «{task_name}»\n\n"
        f"Сверьте результат с описанием проверяемой задачи пункт за пунктом.\n"
        f"Результат будет отправлен в личные сообщения.\n\n"
        f"Описание проверяемой задачи:\n"
        f"{task_text_orig}\n\n"
        f"{excl_line}\n"
        f"Конечный результат: Заполненная таблица ошибок\n"
        f"Лимит времени: {cfg.review_time_limit_min} минут\n"
        f"Лимит доработки: {cfg.rework_limit_hours} час"
    ).strip()

    fields: dict = {
        "Задача": task_links,
        "Режим": "Проверка",
        "Тип проверки": "Проверка ассистентом",
        "Группа": group,
        "Лимит (час)": limit_hours,
        "Для отправки": review_text,
        "Статус": "Очередь",
    }

    await _rate_limit()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _table_url(),
                headers=_headers(),
                json={"fields": fields},
            ) as resp:
                data = await _handle(resp)
                if data and isinstance(data, dict):
                    new_id = data.get("id")
                    logger.info(
                        "Airtable: создана Проверка-итерация %s (parent=%s, group=%s, "
                        "limit=%.4fч, excluded=@%s)",
                        new_id, parent_record_id, group, limit_hours, excl or "—",
                    )
                    return {
                        "record_id": new_id,
                        "group": group,
                        "task_name": f"Проверка задачи «{task_name}»",
                        "task_text": review_text,
                        "limit_hours": limit_hours,
                        "excluded_username": excl,
                        "parent_record_id": parent_record_id,
                    }
    except Exception as e:
        logger.error("create_review_iteration POST failed: %s", e)
    return None


async def fetch_parent_result(parent_record_id: str) -> str | None:
    """Читает Результат с родительской Выполнение-итерации. Используется
    при назначении проверяющего: ему DM-ится этот текст."""
    parent = await fetch_record(parent_record_id)
    if not parent:
        return None
    pf = parent.get("fields", {})
    return pf.get("Результат") or None


async def update_question_answer(question_id: str, answer_text: str) -> bool:
    """PATCH в «Вопросы»/<question_id> — пишет поле «Ответ».
    «Дата ответа» обновится автоматически (lastModifiedTime)."""
    table_url = f"{_BASE}/{cfg.airtable_base_id}/{quote(_QUESTIONS_TABLE, safe='')}/{question_id}"
    await _rate_limit()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                table_url,
                headers=_headers(),
                json={"fields": {"Ответ": answer_text}},
            ) as resp:
                data = await _handle(resp)
                if data:
                    logger.info("Airtable: ответ записан в вопрос %s", question_id)
                    return True
    except Exception as e:
        logger.error("update_question_answer(%s) failed: %s", question_id, e)
    return False
