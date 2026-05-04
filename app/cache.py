"""
Кеш: Redis (основной) или in-memory dict (fallback для тестов).

При пустом REDIS_URL автоматически используется in-memory хранилище.
"""
from __future__ import annotations

import json
import logging
import time
from threading import Lock

logger = logging.getLogger(__name__)

_use_redis: bool = False
_redis_client = None

# In-memory fallback
_memory_store: dict = {}
_memory_lock = Lock()


def is_redis() -> bool:
    return _use_redis


async def init_client(url: str | None = None) -> None:
    global _use_redis, _redis_client

    if not url or not url.strip():
        _use_redis = False
        logger.info("In-memory cache fallback (Redis not configured)")
        return

    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(url, encoding="utf-8", decode_responses=True)
        await _redis_client.ping()
        _use_redis = True
        logger.info("Redis connected: %s", url)
    except Exception as e:
        _use_redis = False
        _redis_client = None
        logger.warning("Redis connection failed: %s. Using in-memory fallback.", e)


async def close_client() -> None:
    global _use_redis, _redis_client
    if _redis_client:
        await _redis_client.close()
        _use_redis = False
        _redis_client = None


# ── Внутренние хелперы ─────────────────────────────────────────────────────────

async def _redis_set(key: str, value: str, ex: int | None = None) -> None:
    if _redis_client:
        await _redis_client.set(key, value, ex=ex)
    else:
        with _memory_lock:
            _memory_store[key] = {
                "value": value,
                "expires": time.time() + ex if ex else None,
            }


async def _redis_get(key: str) -> str | None:
    if _redis_client:
        return await _redis_client.get(key)
    else:
        with _memory_lock:
            entry = _memory_store.get(key)
            if entry is None:
                return None
            if entry["expires"] and time.time() > entry["expires"]:
                del _memory_store[key]
                return None
            return entry["value"]


async def _redis_del(key: str) -> None:
    if _redis_client:
        await _redis_client.delete(key)
    else:
        with _memory_lock:
            _memory_store.pop(key, None)


async def _redis_exists(key: str) -> bool:
    if _redis_client:
        return await _redis_client.exists(key) > 0
    else:
        with _memory_lock:
            entry = _memory_store.get(key)
            if entry is None:
                return False
            if entry["expires"] and time.time() > entry["expires"]:
                del _memory_store[key]
                return False
            return True


async def _redis_scan_delete(pattern: str) -> int:
    if _redis_client:
        deleted = 0
        cursor = 0
        while True:
            cursor, keys = await _redis_client.scan(cursor, match=pattern, count=100)
            if keys:
                deleted += await _redis_client.delete(*keys)
            if cursor == 0:
                break
        return deleted
    else:
        with _memory_lock:
            import fnmatch
            keys_to_del = [k for k in _memory_store if fnmatch.fnmatch(k, pattern)]
            for k in keys_to_del:
                del _memory_store[k]
            return len(keys_to_del)


# ── Публичные хелперы ──────────────────────────────────────────────────────────

async def set_stale_notified(record_id: str, ttl: int | None = None) -> None:
    """Дедуп stale-уведомления: уведомление должно прозвучать ровно один
    раз за «жизнь» задачи. Ключ снимается при publish/restale/cancel/take/
    auto-cancel-missing — поэтому TTL не нужен. Раньше TTL=24ч приводил
    к ложному повтору каждые сутки для забытых задач."""
    await _redis_set(f"stale_notified:{record_id}", "1", ex=ttl)


async def is_stale_notified(record_id: str) -> bool:
    return await _redis_exists(f"stale_notified:{record_id}")


async def del_stale_notified(record_id: str) -> None:
    await _redis_del(f"stale_notified:{record_id}")


async def set_awaiting_reject_reason(moderator_id: int, record_id: str, user_id: int, ttl: int = 600) -> None:
    await _redis_set(f"awaiting_reject_reason:{moderator_id}", f"{record_id}:{user_id}", ex=ttl)


async def get_awaiting_reject_reason(moderator_id: int) -> str | None:
    return await _redis_get(f"awaiting_reject_reason:{moderator_id}")


async def del_awaiting_reject_reason(moderator_id: int) -> None:
    await _redis_del(f"awaiting_reject_reason:{moderator_id}")


async def set_awaiting_task_count(moderator_id: int, ttl: int = 300) -> None:
    await _redis_set(f"awaiting_task_count:{moderator_id}", "1", ex=ttl)


async def get_awaiting_task_count(moderator_id: int) -> str | None:
    return await _redis_get(f"awaiting_task_count:{moderator_id}")


async def del_awaiting_task_count(moderator_id: int) -> None:
    await _redis_del(f"awaiting_task_count:{moderator_id}")


async def set_result_text(record_id: str, text: str, ttl: int = 86400) -> None:
    await _redis_set(f"result_text:{record_id}", text, ex=ttl)


async def get_result_text(record_id: str) -> str | None:
    return await _redis_get(f"result_text:{record_id}")


async def del_result_text(record_id: str) -> None:
    await _redis_del(f"result_text:{record_id}")


async def set_moderator_message_id(record_id: str, message_id: int, ttl: int = 86400) -> None:
    await _redis_set(f"moderator_message_id:{record_id}", str(message_id), ex=ttl)


async def get_moderator_message_id(record_id: str) -> int | None:
    val = await _redis_get(f"moderator_message_id:{record_id}")
    return int(val) if val else None


async def del_moderator_message_id(record_id: str) -> None:
    await _redis_del(f"moderator_message_id:{record_id}")


async def is_moderation_reminder_sent(record_id: str, tier: str) -> bool:
    return await _redis_exists(f"moderation_reminder:{record_id}:{tier}")


async def set_moderation_reminder_sent(record_id: str, tier: str, ttl: int = 48 * 3600) -> None:
    await _redis_set(f"moderation_reminder:{record_id}:{tier}", "1", ex=ttl)


async def clear_all_test_data() -> int:
    total = 0
    for pattern in [
        "stale_notified:*",
        "result_text:*",
        "awaiting_reject_reason:*",
        "awaiting_task_count:*",
        "moderator_message_id:*",
        "moderation_reminder:*",
        "submit_phase:*",
        "result_parts:*",
        "addendum_parts:*",
        "awaiting_msg_to_user:*",
        "awaiting_question:*",
        "pending_question:*",
        "awaiting_question_answer:*",
        "limit_notified:*",
    ]:
        total += await _redis_scan_delete(pattern)
    return total


# ── B-group: накопитель результата и фазы сдачи ───────────────────────────────

# Фаза сдачи результата (per-user, потому что у юзера в моменте одна задача).
# Значения: 'open' (нажал «📝 Сдать результат», копит части),
#           'submitted' (отправил, новые сообщения копятся в дозалив).
# Отсутствие ключа = «нет фазы», DM-сообщения отбиваются с инструкцией.

async def set_submit_phase(user_id: int, phase: str, ttl: int = 86400) -> None:
    await _redis_set(f"submit_phase:{user_id}", phase, ex=ttl)


async def get_submit_phase(user_id: int) -> str | None:
    return await _redis_get(f"submit_phase:{user_id}")


async def del_submit_phase(user_id: int) -> None:
    await _redis_del(f"submit_phase:{user_id}")


async def _append_part(key: str, part: dict, ttl: int) -> None:
    """Унифицированный append к JSON-списку под ключом. Работает и в Redis,
    и в in-memory: считываем JSON, добавляем элемент, кладём обратно."""
    raw = await _redis_get(key)
    parts = json.loads(raw) if raw else []
    parts.append(part)
    await _redis_set(key, json.dumps(parts, ensure_ascii=False), ex=ttl)


async def add_result_part(record_id: str, part: dict, ttl: int = 86400) -> None:
    """Добавить часть в накопитель результата (фаза 'open')."""
    await _append_part(f"result_parts:{record_id}", part, ttl)


async def get_result_parts(record_id: str) -> list[dict]:
    raw = await _redis_get(f"result_parts:{record_id}")
    return json.loads(raw) if raw else []


async def clear_result_parts(record_id: str) -> None:
    await _redis_del(f"result_parts:{record_id}")


async def add_addendum_part(record_id: str, part: dict, ttl: int = 86400) -> None:
    """Добавить часть в дозалив (фаза 'submitted')."""
    await _append_part(f"addendum_parts:{record_id}", part, ttl)


async def get_addendum_parts(record_id: str) -> list[dict]:
    raw = await _redis_get(f"addendum_parts:{record_id}")
    return json.loads(raw) if raw else []


async def clear_addendum_parts(record_id: str) -> None:
    await _redis_del(f"addendum_parts:{record_id}")


# ── B-group: модератор → исполнитель (личное сообщение) ────────────────────────

async def set_awaiting_msg_to_user(
    moderator_id: int, target_user_id: int, record_id: str, ttl: int = 600,
) -> None:
    await _redis_set(
        f"awaiting_msg_to_user:{moderator_id}",
        f"{target_user_id}:{record_id}",
        ex=ttl,
    )


async def get_awaiting_msg_to_user(moderator_id: int) -> str | None:
    """Возвращает '<user_id>:<record_id>' или None."""
    return await _redis_get(f"awaiting_msg_to_user:{moderator_id}")


async def del_awaiting_msg_to_user(moderator_id: int) -> None:
    await _redis_del(f"awaiting_msg_to_user:{moderator_id}")


# ── B-group: исполнитель → модератор (вопрос) ──────────────────────────────────

async def set_awaiting_question(user_id: int, record_id: str, ttl: int = 600) -> None:
    await _redis_set(f"awaiting_question:{user_id}", record_id, ex=ttl)


async def get_awaiting_question(user_id: int) -> str | None:
    return await _redis_get(f"awaiting_question:{user_id}")


async def del_awaiting_question(user_id: int) -> None:
    await _redis_del(f"awaiting_question:{user_id}")


async def set_pending_question(
    user_id: int, record_id: str, text: str, ttl: int = 600,
) -> None:
    """Между вводом текста вопроса и нажатием «Блокирующий?»."""
    payload = json.dumps({"record_id": record_id, "text": text}, ensure_ascii=False)
    await _redis_set(f"pending_question:{user_id}", payload, ex=ttl)


async def get_pending_question(user_id: int) -> dict | None:
    raw = await _redis_get(f"pending_question:{user_id}")
    return json.loads(raw) if raw else None


async def del_pending_question(user_id: int) -> None:
    await _redis_del(f"pending_question:{user_id}")


async def set_awaiting_question_answer(
    moderator_id: int, question_record_id: str, executor_user_id: int, ttl: int = 600,
) -> None:
    await _redis_set(
        f"awaiting_question_answer:{moderator_id}",
        f"{question_record_id}:{executor_user_id}",
        ex=ttl,
    )


async def get_awaiting_question_answer(moderator_id: int) -> str | None:
    """Возвращает '<question_record_id>:<executor_user_id>' или None."""
    return await _redis_get(f"awaiting_question_answer:{moderator_id}")


async def del_awaiting_question_answer(moderator_id: int) -> None:
    await _redis_del(f"awaiting_question_answer:{moderator_id}")


# ── B-group: уведомления о лимите ──────────────────────────────────────────────

async def is_limit_notified(record_id: str, tier: str) -> bool:
    return await _redis_exists(f"limit_notified:{record_id}:{tier}")


async def set_limit_notified(record_id: str, tier: str, ttl: int = 48 * 3600) -> None:
    await _redis_set(f"limit_notified:{record_id}:{tier}", "1", ex=ttl)


async def del_limit_notified_all(record_id: str) -> None:
    """Сбросить все тиры лимита по задаче (используется при отмене/завершении)."""
    for tier in ("50", "80", "100"):
        await _redis_del(f"limit_notified:{record_id}:{tier}")
