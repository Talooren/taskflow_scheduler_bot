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


async def get_assistant_record_id(username: str | None) -> str | None:
    """Публичная обёртка: record_id исполнителя по полю Телеграм в таблице
    «Исполнители». Используется в on_reaction (проверка права на взятие) и
    в /start (детект роли). None — если username пустой или не найден."""
    return await _lookup_record_id_by_telegram("Исполнители", username) if username else None


async def get_team_record_id(username: str | None) -> str | None:
    """Публичная обёртка: record_id члена команды по Телеграм в таблице «Команда»."""
    return await _lookup_record_id_by_telegram("Команда", username) if username else None


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
