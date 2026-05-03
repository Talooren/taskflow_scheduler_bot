"""
Сдача результата (накопитель + дозалив) и вопросы исполнителя.

DM-сообщения от исполнителя теперь обрабатываются по фазам:
  awaiting_question        → текст вопроса (FSM создания записи в «Вопросы»)
  submit_phase = 'open'    → копит части в result_parts (накопитель)
  submit_phase = 'submitted' → копит части в addendum_parts (дозалив)
  ничего из перечисленного → подсказка нажать /status → «📝 Сдать результат»

После «✅ Отправить» — форвард всех частей в группу модераторов + карточка
с [Принять/Отклонить/Написать]. Содержимое склеивается в pending_results.result_content
(SR-2: переживает рестарт через PG, прогрев Redis в run.py).
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from app import airtable, cache, db
from app.config import cfg
from app.keyboards import (
    accept_reject_kb,
    addendum_kb,
    blocking_choice_kb,
    question_card_kb,
    result_acc_kb,
)

logger = logging.getLogger(__name__)
router = Router()


# ── Главный диспетчер DM-сообщений от исполнителя ──────────────────────────────

@router.message(F.chat.type == "private")
async def handle_private(message: Message, bot: Bot) -> None:
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id

    # 1. Если ждём текст вопроса от исполнителя — обрабатываем как вопрос.
    awaiting_q_rid = await cache.get_awaiting_question(user_id)
    if awaiting_q_rid:
        await _handle_question_text(message, awaiting_q_rid)
        return

    pending = await db.get_pending_result(user_id)
    if not pending:
        await message.answer(
            "У вас нет активной задачи.\n"
            "Поставьте реакцию на задачу в группе, чтобы взяться за неё.\n"
            "/status — проверить статус"
        )
        return

    record_id = pending["record_id"]
    phase = await cache.get_submit_phase(user_id)

    # 2. Накопитель (фаза 'open')
    if phase == "open":
        await _handle_accumulator_message(message, bot, pending)
        return

    # 3. Дозалив (фаза 'submitted')
    if phase == "submitted":
        await _handle_addendum_message(message, bot, pending)
        return

    # 4. Фаза не активна — просим явно нажать кнопку.
    await message.answer(
        "📝 Чтобы сдать результат — откройте /status и нажмите "
        "«📝 Сдать результат». Потом присылайте сообщения, "
        "и в конце жмите «✅ Отправить»."
    )


# ── Накопитель (open) ──────────────────────────────────────────────────────────

async def _handle_accumulator_message(
    message: Message, bot: Bot, pending: dict,
) -> None:
    record_id = pending["record_id"]

    content, result_type = await _extract_result_content(message, bot)
    if result_type is None:
        await message.answer(
            "Этот формат не поддерживается. Пришли результат текстом, фото, "
            "документом, видео, голосовым или файлом. Для GIF — перешли как "
            "документ/видео."
        )
        return

    part = {
        "chat_id": message.chat.id,
        "msg_id": message.message_id,
        "content": content,
        "type": result_type,
    }
    await cache.add_result_part(record_id, part)

    parts = await cache.get_result_parts(record_id)
    n = len(parts)
    await message.answer(
        f"✅ Принял часть {n}. Можно прислать ещё или отправить пакет модератору.",
        reply_markup=result_acc_kb(record_id),
    )


# ── Дозалив (submitted) ────────────────────────────────────────────────────────

async def _handle_addendum_message(
    message: Message, bot: Bot, pending: dict,
) -> None:
    record_id = pending["record_id"]

    content, result_type = await _extract_result_content(message, bot)
    if result_type is None:
        await message.answer(
            "Этот формат не поддерживается. Текст, фото, документ, видео, "
            "голосовое или файл — пришли заново."
        )
        return

    part = {
        "chat_id": message.chat.id,
        "msg_id": message.message_id,
        "content": content,
        "type": result_type,
    }
    await cache.add_addendum_part(record_id, part)

    parts = await cache.get_addendum_parts(record_id)
    n = len(parts)
    await message.answer(
        f"📨 В дозаливе {n} часть(и). Жми «Доотправить», когда готов.",
        reply_markup=addendum_kb(record_id),
    )


# ── Callbacks: запуск накопителя и вопрос ──────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("submit_"))
async def on_submit_start(callback: CallbackQuery) -> None:
    """Кнопка «📝 Сдать результат» из /status. Включает фазу 'open'."""
    record_id = callback.data.split("submit_", 1)[1]
    user_id = callback.from_user.id

    pending = await db.get_pending_result(user_id)
    if not pending or pending["record_id"] != record_id:
        await callback.answer("Эта задача уже не за тобой.", show_alert=True)
        return

    # Если уже была отправка — возвращаем в 'open' (модератор ещё не принял),
    # старые parts чистим (отправили — забыли).
    await cache.clear_result_parts(record_id)
    await cache.set_submit_phase(user_id, "open")
    await callback.message.answer(
        "📝 Режим сдачи результата включён.\n\n"
        "Присылай сообщения (текст / фото / файл / голосовое — можно несколько). "
        "После каждого появятся кнопки управления. "
        "Когда всё готово — жми «✅ Отправить»."
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("ask_"))
async def on_ask_start(callback: CallbackQuery) -> None:
    """Кнопка «❓ Задать вопрос» из /status. Ждём текст вопроса."""
    record_id = callback.data.split("ask_", 1)[1]
    user_id = callback.from_user.id

    pending = await db.get_pending_result(user_id)
    if not pending or pending["record_id"] != record_id:
        await callback.answer("Эта задача уже не за тобой.", show_alert=True)
        return

    await cache.set_awaiting_question(user_id, record_id)
    await callback.message.answer(
        "❓ Напиши текст вопроса. Я перешлю его модераторам и сохраню в Airtable «Вопросы»."
    )
    await callback.answer()


# ── Вопросы: ввод текста и выбор «Блокирующий?» ────────────────────────────────

async def _handle_question_text(message: Message, record_id: str) -> None:
    """Получили текст вопроса — сохраняем во временный кэш и просим выбрать
    блокирующий/нет."""
    user_id = message.from_user.id
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("Вопрос должен быть текстовым. Напиши вопрос текстом.")
        return

    await cache.del_awaiting_question(user_id)
    await cache.set_pending_question(user_id, record_id, text)
    await message.answer(
        f"Вопрос:\n«{text}»\n\nБлокирует ли он работу?",
        reply_markup=blocking_choice_kb(record_id),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("qblk_"))
async def on_blocking_choice(callback: CallbackQuery, bot: Bot) -> None:
    """Колбэк выбора Блокирующий/Не блокирующий после ввода текста вопроса.
    Создаёт запись в Airtable «Вопросы» и шлёт карточку в группу модераторов."""
    user_id = callback.from_user.id
    payload = callback.data.split("qblk_", 1)[1]
    try:
        record_id, blocking_flag = payload.rsplit("_", 1)
        blocking = blocking_flag == "1"
    except ValueError:
        await callback.answer("Неверный формат.", show_alert=True)
        return

    pending_q = await cache.get_pending_question(user_id)
    if not pending_q or pending_q.get("record_id") != record_id:
        await callback.answer("Текст вопроса утерян, начни заново.", show_alert=True)
        return

    text = pending_q["text"]
    username = callback.from_user.username or callback.from_user.first_name or str(user_id)

    # Создаём запись в Airtable
    question_id = await airtable.create_question(record_id, username, text, blocking)

    # Дёргаем pending для контекста (номер задачи)
    pending = await db.get_pending_result(user_id)
    task_num = pending.get("task_number") if pending else "?"
    task_name = pending.get("task_name") if pending else ""

    # Шлём карточку в группу модераторов
    blocking_label = "🚧 БЛОКИРУЮЩИЙ" if blocking else "📨 не блокирующий"
    card_text = (
        f"❓ Вопрос от @{username} по задаче #{task_num} «{task_name}»\n"
        f"{blocking_label}\n\n"
        f"{text}"
    )
    if question_id:
        try:
            await bot.send_message(
                cfg.moderator_group_id,
                card_text,
                reply_markup=question_card_kb(question_id, user_id),
            )
        except Exception as e:
            logger.error("Не удалось отправить вопрос модераторам: %s", e)
    else:
        # Airtable не сработал — шлём в группу без кнопки «Ответить» с пометкой
        try:
            await bot.send_message(
                cfg.moderator_group_id,
                card_text + "\n\n⚠️ Не удалось записать в Airtable — ответьте здесь сами.",
            )
        except Exception as e:
            logger.error("Не удалось отправить fallback-вопрос: %s", e)

    await cache.del_pending_question(user_id)

    # Подтверждение исполнителю
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "✅ Вопрос отправлен модераторам. Ответ придёт сюда же."
    )
    await callback.answer()
    logger.info(
        "[question] @%s → %s (record=%s, blocking=%s, question_id=%s)",
        username, text[:60], record_id, blocking, question_id,
    )


# ── Callbacks: накопитель — отправить / сбросить / ещё ─────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("rmore_"))
async def on_result_more(callback: CallbackQuery) -> None:
    """«➕ Жду ещё» — просто прячем кнопки на этом сообщении."""
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Жду следующее сообщение")


@router.callback_query(lambda c: c.data and c.data.startswith("rclear_"))
async def on_result_clear(callback: CallbackQuery) -> None:
    """«🗑 Сбросить» — чистим накопитель, остаёмся в фазе 'open'
    (исполнитель может начать заново)."""
    record_id = callback.data.split("rclear_", 1)[1]
    user_id = callback.from_user.id

    await cache.clear_result_parts(record_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "🗑 Накопитель очищен. Можно начинать заново — присылай сообщения."
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("rsend_"))
async def on_result_send(callback: CallbackQuery, bot: Bot) -> None:
    """«✅ Отправить» — форвардим все части в группу модераторов, шлём
    карточку с [Принять/Отклонить/Написать], переходим в фазу 'submitted'."""
    record_id = callback.data.split("rsend_", 1)[1]
    user_id = callback.from_user.id

    pending = await db.get_pending_result(user_id)
    if not pending or pending["record_id"] != record_id:
        await callback.answer("Эта задача уже не за тобой.", show_alert=True)
        return

    parts = await cache.get_result_parts(record_id)
    if not parts:
        await callback.answer("Накопитель пуст — нечего отправлять.", show_alert=True)
        return

    username = pending.get("username") or callback.from_user.username \
        or callback.from_user.first_name or str(user_id)
    task_num = pending.get("task_number") or "?"
    task_name = pending.get("task_name") or "Задача"

    # 1. Карточка-заголовок с кнопками
    header = (
        f"📨 Результат по задаче #{task_num} — {task_name}\n"
        f"Исполнитель: @{username} (id: {user_id})\n"
        f"Частей в пакете: {len(parts)}"
    )
    try:
        card_msg = await bot.send_message(
            cfg.moderator_group_id,
            header,
            reply_markup=accept_reject_kb(record_id, user_id),
        )
        await cache.set_moderator_message_id(record_id, card_msg.message_id)
    except Exception as e:
        logger.error("Не удалось отправить карточку результата: %s", e)
        await callback.answer("Ошибка отправки. Попробуй ещё раз.", show_alert=True)
        return

    # 2. Форвардим каждую часть
    for p in parts:
        try:
            await bot.forward_message(
                cfg.moderator_group_id,
                from_chat_id=p["chat_id"],
                message_id=p["msg_id"],
            )
        except Exception as e:
            logger.warning("forward part %s failed: %s", p.get("msg_id"), e)

    # 3. Сохраняем содержимое в БД и кэше (для accept → Airtable + warm cache)
    merged_content = "\n\n---\n\n".join(p.get("content", "") for p in parts if p.get("content"))
    await db.set_pending_result_content(record_id, merged_content, "bundle")
    await cache.set_result_text(record_id, merged_content)

    # 4. Переходим в фазу дозалива, чистим накопитель
    await cache.set_submit_phase(user_id, "submitted")
    await cache.clear_result_parts(record_id)
    await cache.clear_addendum_parts(record_id)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "✅ Результат отправлен модераторам.\n\n"
        "Если что-то забыл — пришли сообщение, появится кнопка «📨 Доотправить»."
    )
    await callback.answer("Отправлено")
    logger.info(
        "[result_send] @%s task=%s parts=%d", username, record_id, len(parts),
    )


# ── Callbacks: дозалив — отправить / сбросить ──────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("aclear_"))
async def on_addendum_clear(callback: CallbackQuery) -> None:
    """«🗑 Сбросить дозалив» — выкидываем накопленные части дозалива."""
    record_id = callback.data.split("aclear_", 1)[1]
    await cache.clear_addendum_parts(record_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("🗑 Дозалив очищен.")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("asend_"))
async def on_addendum_send(callback: CallbackQuery, bot: Bot) -> None:
    """«📨 Доотправить» — форвардим дозалив в группу модераторов как
    дополнительные сообщения. Карточку с кнопками не шлём — accept/reject
    делается на исходной карточке. Содержимое дозалива дописываем в
    pending_results.result_content, чтобы accept в итоге улетел в Airtable
    с полным набором."""
    record_id = callback.data.split("asend_", 1)[1]
    user_id = callback.from_user.id

    pending = await db.get_pending_result(user_id)
    if not pending or pending["record_id"] != record_id:
        await callback.answer("Эта задача уже не за тобой.", show_alert=True)
        return

    parts = await cache.get_addendum_parts(record_id)
    if not parts:
        await callback.answer("Дозалив пуст.", show_alert=True)
        return

    username = pending.get("username") or callback.from_user.username \
        or callback.from_user.first_name or str(user_id)
    task_num = pending.get("task_number") or "?"

    # Заголовок дозалива (без кнопок — accept/reject всё ещё на исходной карточке)
    mod_msg_id = await cache.get_moderator_message_id(record_id)
    try:
        await bot.send_message(
            cfg.moderator_group_id,
            f"📨 Дозалив @{username} к задаче #{task_num} (частей: {len(parts)})",
            reply_to_message_id=mod_msg_id if mod_msg_id else None,
        )
    except Exception as e:
        logger.warning("addendum header send failed: %s", e)

    # Форвард частей
    for p in parts:
        try:
            await bot.forward_message(
                cfg.moderator_group_id,
                from_chat_id=p["chat_id"],
                message_id=p["msg_id"],
            )
        except Exception as e:
            logger.warning("forward addendum part %s failed: %s", p.get("msg_id"), e)

    # Дописываем в общий result_content, чтобы accept ушёл с полным контентом
    addendum_text = "\n\n---\n\n".join(
        p.get("content", "") for p in parts if p.get("content")
    )
    if addendum_text:
        existing = await cache.get_result_text(record_id) or ""
        merged = (existing + "\n\n=== ДОЗАЛИВ ===\n\n" + addendum_text) if existing else addendum_text
        await db.set_pending_result_content(record_id, merged, "bundle")
        await cache.set_result_text(record_id, merged)

    await cache.clear_addendum_parts(record_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("📨 Дозалив отправлен модераторам.")
    await callback.answer()
    logger.info(
        "[addendum_send] @%s task=%s parts=%d", username, record_id, len(parts),
    )


# ── Извлечение контента сообщения ──────────────────────────────────────────────

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
