"""
Планировщик: check_stale (30-мин простой до взятия) и
check_pending_moderation (напоминания модератору через 4/12/24 часа).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import cache, db
from app.config import cfg
from app.keyboards import stale_notification_kb

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")
_bot: Bot | None = None

STALE_MINUTES = 30

# SR-3: три уровня напоминаний модератору о непринятом результате.
_MODERATION_TIERS: tuple[tuple[str, float], ...] = (
    ("4h", 4 * 3600),
    ("12h", 12 * 3600),
    ("24h", 24 * 3600),
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


async def check_pending_moderation() -> None:
    """SR-3: каждые 10 минут ищем результаты, которые модератор не принял/
    не отклонил через 4/12/24 часа после получения, и шлём напоминание.
    Дедуп через Redis: по ключу на (record_id, tier), TTL 48ч."""
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

        hours_elapsed = int(elapsed // 3600)
        text = (
            f"⌛ Напоминание: задача #{rec.get('task_number')} — {rec.get('task_name')}\n"
            f"ждёт модерации уже {hours_elapsed} ч.\n"
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
            "[moderation] Напоминание %s отправлено (tier=%s, elapsed≈%dч)",
            record_id, threshold_label, hours_elapsed,
        )


def setup(bot: Bot) -> None:
    global _bot
    _bot = bot

    scheduler.add_job(check_stale, "interval", minutes=1, id="stale", replace_existing=True)
    scheduler.add_job(
        check_pending_moderation,
        "interval",
        minutes=10,
        id="pending_moderation",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("[scheduler] Запущен: check_stale=1min, check_pending_moderation=10min")
