"""
Обработка результатов от исполнителей.

Любое сообщение (не команда) в ЛС — сдача результата.
Пересылается модераторам с inline-кнопками [✅ Принять] [❌ Не принимать].
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from app import cache, db
from app.config import cfg
from app.keyboards import accept_reject_kb

logger = logging.getLogger(__name__)
router = Router()


@router.message(F.chat.type == "private")
async def handle_private(message: Message, bot: Bot) -> None:
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id
    pending = await db.get_pending_result(user_id)

    if not pending:
        await message.answer(
            "У вас нет активной задачи.\n"
            "Поставьте реакцию на задачу в группе, чтобы взяться за неё.\n"
            "/status — проверить статус"
        )
        return

    record_id = pending["record_id"]
    username = pending["username"] or message.from_user.username or message.from_user.first_name or str(user_id)
    task_number = pending["task_number"] or "?"
    task_name = pending["task_name"] or "Задача"

    # Извлечь контент для Airtable
    result_content, result_type = await _extract_result_content(message, bot)
    if result_type is None:
        # animation/contact/location/venue/poll/dice/game и пр. — неподдержанный
        # формат. Задача остаётся в pending_results, исполнитель может переслать.
        await message.answer(
            "Этот формат не поддерживается. Пришли результат текстом, фото, "
            "документом, видео, голосовым или файлом. Для GIF — перешли как "
            "документ/видео."
        )
        return
    # PostgreSQL — источник правды (переживает Redis TTL 24ч); Redis — кэш.
    await db.set_pending_result_content(record_id, result_content, result_type)
    await cache.set_result_text(record_id, result_content)

    # Карточка для модераторов
    header = (
        f"📨 Результат по задаче #{task_number} — {task_name}\n"
        f"Исполнитель: @{username} (id: {user_id})"
    )

    try:
        card_msg = await bot.send_message(
            cfg.moderator_group_id,
            header,
            reply_markup=accept_reject_kb(record_id, user_id),
        )
        # Пересылаем оригинальное сообщение
        await bot.forward_message(
            cfg.moderator_group_id,
            message.chat.id,
            message.message_id,
        )
        # Сохраняем message_id карточки для редактирования
        await cache.set_moderator_message_id(record_id, card_msg.message_id)
    except Exception as e:
        logger.error("Не удалось уведомить модераторов: %s", e)

    await message.answer(
        "Результат отправлен модераторам. Ожидайте подтверждения."
    )

    logger.info(
        "[executor] @%s (id=%s) сдал результат по задаче %s",
        username, user_id, record_id,
    )


_MEDIA_TOKEN_WARNED = False


def _warn_media_token_once() -> None:
    """Один раз на процесс предупреждаем, что медиа-URL в Airtable
    содержат BOT_TOKEN и живут ограниченное время на стороне Telegram."""
    global _MEDIA_TOKEN_WARNED
    if _MEDIA_TOKEN_WARNED:
        return
    _MEDIA_TOKEN_WARNED = True
    logger.warning(
        "Медиа-ссылки в Airtable содержат BOT_TOKEN и имеют ограниченный "
        "срок жизни на стороне Telegram. Не расшаривай базу Airtable наружу."
    )


async def _extract_result_content(message: Message, bot: Bot) -> tuple[str, str | None]:
    """
    Извлекает контент для записи в Airtable:
    - текст → текст
    - медиа → ссылка + подпись
    Возвращает (content, result_type). result_type ∈ {"text", "media", None}:
    None означает, что сообщение не попало ни в одну известную ветку
    (animation/contact/location/venue/poll/dice/game и т.п.) — вызывающий
    должен отбить его, не принимая как результат.
    """
    file_id = None
    caption = message.caption or ""

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.audio:
        file_id = message.audio.file_id
    elif message.voice:
        file_id = message.voice.file_id
    elif message.video_note:
        file_id = message.video_note.file_id
    elif message.sticker:
        file_id = message.sticker.file_id

    if file_id:
        _warn_media_token_once()
        try:
            file = await bot.get_file(file_id)
            file_url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
            content = f"{file_url}\n{caption}" if caption else file_url
            return content, "media"
        except Exception as e:
            logger.error("get_file failed: %s", e)
            file_url = "[файл недоступен]"
            content = f"{file_url}\n{caption}" if caption else file_url
            return content, "media"

    text = message.text or message.caption
    if text:
        return text, "text"

    # Ни file_id, ни текста/caption — неподдерживаемый тип.
    return "", None
