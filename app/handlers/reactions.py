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
    # В TEST_MODE реакции не обрабатываем
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

    await _assign_user(user.id, user.username or user.first_name or str(user.id), task["record_id"], bot)
