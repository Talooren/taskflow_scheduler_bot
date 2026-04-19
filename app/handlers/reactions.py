"""
Обработка реакций на сообщения (message_reaction).

Первый пользователь, поставивший реакцию на сообщение с задачей,
становится исполнителем.

В TEST_MODE не используется — там кнопка [👤 Взять задачу (тест)].
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import MessageReactionUpdated

from app import airtable, db
from app.config import cfg
from app.handlers.moderator import _assign_user

logger = logging.getLogger(__name__)
router = Router()


@router.message_reaction()
async def on_reaction(event: MessageReactionUpdated, bot: Bot) -> None:
    # В TEST_MODE реакции не обрабатываем — там кнопка «👤 Взять задачу (тест)»
    if cfg.test_mode:
        return

    if not event.new_reaction:
        return
    if not event.user:
        return

    chat_id = event.chat.id
    message_id = event.message_id
    user = event.user

    task = await db.get_task_by_message(chat_id, message_id)
    if not task:
        return

    if await db.has_pending_result(user.id):
        return

    # Whitelist: исполнитель должен быть в таблице Airtable «Исполнители»,
    # иначе случайный гость группы сможет «взять» задачу.
    if not user.username:
        try:
            await bot.send_message(
                user.id,
                "Чтобы брать задачи, установи username в настройках Telegram "
                "и попроси модератора зарегистрировать тебя в таблице «Исполнители».",
            )
        except Exception:
            pass  # юзер не открывал ЛС с ботом — ничего не делаем
        logger.info("[reaction] отклонено id=%s: нет Telegram username", user.id)
        return

    assistant_rec = await airtable.get_assistant_record_id(user.username)
    if not assistant_rec:
        # Уведомить самого юзера (если открыт ЛС с ботом)
        try:
            await bot.send_message(
                user.id,
                f"@{user.username}, ты не зарегистрирован(а) как исполнитель.\n\n"
                f"Попроси модератора добавить тебя в таблицу «Исполнители» "
                f"Airtable: поле «Телеграм» = @{user.username}.\n\n"
                f"Реакция на задачу не сработает до регистрации.",
            )
        except Exception:
            pass
        # Уведомить модераторов
        try:
            await bot.send_message(
                cfg.moderator_group_id,
                f"⚠️ @{user.username} (id={user.id}) пытался(-лась) взять задачу "
                f"#{task['task_number']}, но не зарегистрирован(а) в «Исполнители».\n"
                f"Заведите в Airtable → таблица «Исполнители» → Телеграм=@{user.username}.",
            )
        except Exception as e:
            logger.warning("[reaction] не удалось уведомить модераторов: %s", e)
        logger.info(
            "[reaction] @%s (id=%s) отклонён: нет в «Исполнители»; задача %s осталась published",
            user.username, user.id, task["record_id"],
        )
        return

    await _assign_user(
        user.id,
        user.username,
        task["record_id"],
        bot,
    )
