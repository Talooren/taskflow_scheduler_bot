"""
Планировщик: check_stale (30-мин простой до взятия) и
check_pending_moderation (напоминания модератору через 4/12/24 часа).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import airtable, cache, db
from app.config import cfg
from app.keyboards import stale_notification_kb

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")
_bot: Bot | None = None

STALE_MINUTES = 30

# Уровни напоминаний модератору о непринятом результате (label, threshold_sec).
# Значения должны быть упорядочены по возрастанию. Label используется в ключе
# Redis-дедупа (moderation_reminder:{record_id}:{label}).
_MODERATION_TIERS: tuple[tuple[str, float], ...] = (
    ("15m",  15 * 60),
    ("30m",  30 * 60),
    ("45m",  45 * 60),
    ("1h",   60 * 60),
    ("2h",  120 * 60),
)


def _to_utc_dt(value) -> datetime | None:
    """Нормализует assigned_at/result_received_at: PG отдаёт datetime,
    SQLite — ISO-строку."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def check_stale() -> None:
    """Каждую минуту: проверяем задачи published > 30 мин без исполнителя."""
    stale = await db.get_stale_published(STALE_MINUTES)
    if not stale:
        return

    for task in stale:
        record_id = task["record_id"]

        # Уже уведомляли?
        if await cache.is_stale_notified(record_id):
            continue

        # Отправляем уведомление в группу модераторов
        try:
            msg = await _bot.send_message(
                cfg.moderator_group_id,
                f"⏰ Задача #{task['task_number']} — {task['task_name']}\n"
                f"не взята уже 30 минут. Опубликовать повторно?",
                reply_markup=stale_notification_kb(record_id),
            )
        except Exception as e:
            logger.error("[stale] Не удалось отправить уведомление %s: %s", record_id, e)
            continue

        await cache.set_stale_notified(record_id)
        logger.info("[stale] Уведомление отправлено для %s", record_id)


async def check_stale_force() -> None:
    """Принудительный запуск — игнорирует 30-минутный порог (для /test_skip_stale)."""
    # Порог = 0 минут → все опубликованные считаем просроченными.
    # Работает и в PG, и в SQLite через общую обёртку.
    stale = await db.get_stale_published(0)

    for task in stale:
        record_id = task["record_id"]
        if await cache.is_stale_notified(record_id):
            continue

        try:
            await _bot.send_message(
                cfg.moderator_group_id,
                f"⏰ Задача #{task['task_number']} — {task['task_name']}\n"
                f"не взята. Опубликовать повторно?",
                reply_markup=stale_notification_kb(record_id),
            )
        except Exception as e:
            logger.error("[stale force] Ошибка %s: %s", record_id, e)
            continue

        await cache.set_stale_notified(record_id)
        logger.info("[stale force] Уведомление отправлено для %s", record_id)


def _format_elapsed(seconds: float) -> str:
    """Человекочитаемое время: 'Xм', 'Yч', 'Yч Xм'."""
    total_min = int(seconds // 60)
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h}ч {m}мин"
    if h:
        return f"{h}ч"
    return f"{m}мин"


async def check_pending_moderation() -> None:
    """Каждые 5 минут ищем результаты, которые модератор не принял/не отклонил,
    и шлём напоминание при переходе через 15/30/45 мин / 1 ч / 2 ч после
    получения результата. Дедуп через Redis: по ключу (record_id, tier), TTL 48ч."""
    records = await db.list_pending_with_result()
    if not records:
        return

    now = datetime.now(timezone.utc)

    for rec in records:
        received = _to_utc_dt(rec.get("result_received_at"))
        if received is None:
            continue
        elapsed = (now - received).total_seconds()
        if elapsed < _MODERATION_TIERS[0][1]:
            continue

        record_id = rec["record_id"]
        tier_to_send: str | None = None
        threshold_label: str | None = None
        # Берём самый «старший» ещё не отправленный tier, порог которого пройден.
        for label, threshold in _MODERATION_TIERS:
            if elapsed >= threshold and not await cache.is_moderation_reminder_sent(record_id, label):
                tier_to_send = label
                threshold_label = label
        if not tier_to_send:
            continue

        elapsed_str = _format_elapsed(elapsed)
        text = (
            f"⌛ Напоминание: задача #{rec.get('task_number')} — {rec.get('task_name')}\n"
            f"ждёт модерации уже {elapsed_str}.\n"
            f"Исполнитель: @{rec.get('username')}"
        )

        reply_to = await cache.get_moderator_message_id(record_id)

        try:
            await _bot.send_message(
                cfg.moderator_group_id,
                text,
                reply_to_message_id=reply_to if reply_to else None,
            )
        except Exception as e:
            logger.error("[moderation] Не удалось отправить напоминание %s: %s", record_id, e)
            continue

        await cache.set_moderation_reminder_sent(record_id, tier_to_send)
        logger.info(
            "[moderation] Напоминание %s отправлено (tier=%s, elapsed≈%s)",
            record_id, threshold_label, elapsed_str,
        )


async def refresh_moderators() -> None:
    """Обновляет cfg.moderator_usernames из таблицы «Команда» Airtable.
    Позволяет добавлять/удалять модераторов без рестарта бота."""
    try:
        fresh = await airtable.fetch_all_team_telegrams()
        cfg.moderator_usernames = fresh
        logger.info("[scheduler] moderator_usernames refreshed: %d", len(fresh))
    except Exception as e:
        logger.warning("[scheduler] refresh_moderators failed: %s", e)


async def _auto_cancel_missing(task: dict) -> None:
    """Авто-отмена задачи, которую удалили в Airtable вручную.

    Делает то же, что ручная кнопка «🗑 Отменить», но БЕЗ записи в Airtable
    (записи уже нет) и С уведомлением в группу модераторов о факте удаления.
    """
    record_id = task["record_id"]
    status = task.get("status")

    # 1. Удалить сообщение в рабочей группе
    if status in ("published", "assigned") and task.get("chat_id") and task.get("message_id"):
        try:
            await _bot.delete_message(
                chat_id=task["chat_id"], message_id=task["message_id"],
            )
        except Exception as e:
            logger.warning(
                "[sync] delete_message(%s/%s) failed: %s",
                task["chat_id"], task["message_id"], e,
            )

    # 2. Уведомить исполнителя в ЛС, если задача уже была взята
    if status == "assigned":
        pending = await db.get_pending_result_by_record(record_id)
        if pending:
            try:
                await _bot.send_message(
                    pending["user_id"],
                    f"❌ Задача #{task.get('task_number')} "
                    f"«{task.get('task_name')}» удалена в Airtable модератором. "
                    f"Работу можно прекратить.",
                )
            except Exception as e:
                logger.warning("[sync] DM executor failed: %s", e)
            await db.delete_pending_result(record_id)
            await cache.del_result_text(record_id)
            await cache.del_moderator_message_id(record_id)

    # 3. Обновить БД и кэш
    await db.update_task_cancelled(record_id)
    await cache.del_stale_notified(record_id)

    # 4. Уведомить группу модераторов
    try:
        await _bot.send_message(
            cfg.moderator_group_id,
            f"🗑 Задача #{task.get('task_number')} «{task.get('task_name')}» "
            f"удалена в Airtable вручную — авто-отменена ботом.",
        )
    except Exception as e:
        logger.warning("[sync] notify moderators failed: %s", e)


async def sync_with_airtable() -> None:
    """Сверяет активные задачи в БД с Airtable. Если запись точно удалена
    (HTTP 404), задача авто-отменяется. Транзиентные ошибки (None от
    record_exists) НЕ приводят к отмене — это защита от массового удаления
    при сетевом блипе/rate-limit/5xx Airtable.

    Запуск раз в 5 минут — компромисс между свежестью и нагрузкой на API
    (один GET на каждую активную задачу). При большом количестве активных
    задач (сотни) можно увеличить интервал.
    """
    tasks = await db.get_all_active_tasks()
    if not tasks:
        return

    cancelled = 0
    for task in tasks:
        if task.get("status") in ("done", "cancelled"):
            continue
        record_id = task["record_id"]
        exists = await airtable.record_exists(record_id)
        if exists is False:
            logger.info("[sync] %s удалена в Airtable — авто-отмена", record_id)
            await _auto_cancel_missing(task)
            cancelled += 1
        # exists is True/None — не трогаем

    if cancelled:
        logger.info("[sync] auto-cancelled %d task(s)", cancelled)


def setup(bot: Bot) -> None:
    global _bot
    _bot = bot

    scheduler.add_job(check_stale, "interval", minutes=1, id="stale", replace_existing=True)
    scheduler.add_job(
        check_pending_moderation,
        "interval",
        minutes=5,
        id="pending_moderation",
        replace_existing=True,
    )
    scheduler.add_job(
        refresh_moderators,
        "interval",
        minutes=5,
        id="mod_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        sync_with_airtable,
        "interval",
        minutes=5,
        id="airtable_sync",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "[scheduler] Запущен: check_stale=1min, check_pending_moderation=5min, "
        "refresh_moderators=5min, sync_with_airtable=5min"
    )
