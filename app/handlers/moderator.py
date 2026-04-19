"""
Панель модератора и управление задачами.

Команды:
  /панель, /panel — открыть панель
Callbacks:
  load_tasks, clear_queue, refresh_schedule
  publish_{record_id}, take_test_{record_id}
  accept_{record_id}_{user_id}, reject_{record_id}_{user_id}
  restale_{record_id}, skip_stale_{record_id}
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import airtable, cache, db
from app.config import cfg
from app.keyboards import (
    accept_reject_kb,
    build_publish_keyboard,
    moderator_panel_kb,
    publish_task_kb,
    stale_notification_kb,
)
from app.utils import utc_now_iso

logger = logging.getLogger(__name__)
router = Router()


def _clean_task_name(fields: dict) -> str:
    """Получить человекочитаемое название из Airtable-полей.

    Поле «Название задачи» (multipleLookupValues) — lookup из таблицы
    Задачи — даёт чистое короткое название. «Этап» — formula, клеит
    статус + режим + название + номер («Очередь Выполнение - ... (102)»)
    и визуально перегружает UI. Берём первое — фоллбэк на второе.
    """
    name_lookup = fields.get("Название задачи")
    if isinstance(name_lookup, list) and name_lookup:
        return str(name_lookup[0]).strip()
    if isinstance(name_lookup, str) and name_lookup.strip():
        return name_lookup.strip()
    # fallback на Этап
    return fields.get("Этап", "") or ""


# ── Панель модератора ──────────────────────────────────────────────────────────

@router.message(Command("панель", "panel"))
async def cmd_panel(message: Message) -> None:
    if not cfg.is_moderator(message.from_user.id):
        await message.answer("Нет прав.")
        return
    enabled = await db.is_publishing_enabled()
    await message.answer(
        "Панель модератора",
        reply_markup=moderator_panel_kb(enabled),
    )


@router.callback_query(lambda c: c.data == "toggle_publishing")
async def on_toggle_publishing(callback: CallbackQuery) -> None:
    if not cfg.is_moderator(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return
    new_value = not await db.is_publishing_enabled()
    await db.set_publishing(new_value)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=moderator_panel_kb(new_value),
        )
    except Exception:
        pass
    await callback.answer(
        "Публикация включена" if new_value else "Публикация выключена"
    )
    logger.info("[moderator] publishing_enabled -> %s", new_value)


@router.callback_query(lambda c: c.data in ("load_tasks", "clear_queue", "refresh_schedule"))
async def on_panel_action(callback: CallbackQuery) -> None:
    if not cfg.is_moderator(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return

    action = callback.data

    if action == "load_tasks":
        await cache.set_awaiting_task_count(callback.from_user.id)
        await callback.message.answer(
            "Сколько задач загрузить? Введите число:"
        )
        await callback.answer()

    elif action == "clear_queue":
        deleted = await db.delete_tasks_loaded()
        await callback.message.answer(
            f"Очередь задач очищена. Удалено задач: {deleted}.\n"
            f"Задачи в работе не затронуты."
        )
        await callback.answer(f"Очищено: {deleted}")

    elif action == "refresh_schedule":
        loaded = await db.get_tasks_loaded()
        refreshed = 0
        for task in loaded:
            record = await airtable.fetch_record(task["record_id"])
            if record:
                fields = record.get("fields", {})
                await db.update_task_fields(
                    task["record_id"],
                    {
                        "task_name": _clean_task_name(fields) or task["task_name"],
                        "task_text": fields.get("Для отправки", task["task_text"]),
                        "mode": fields.get("Режим"),
                        "limit_hours": fields.get("Лимит (час)", task["limit_hours"]),
                    },
                )
                refreshed += 1
        await callback.message.answer(f"Расписание обновлено. Обновлено задач: {refreshed}")
        await callback.answer("Обновлено")


# ── Ввод количества задач (FSM через Redis) ────────────────────────────────────

async def _awaiting_moderator_input(message: Message) -> bool:
    """Фильтр хэндлера: срабатывает только когда модератор реально
    сейчас вводит ответ на запрос бота (количество задач или причина
    отказа). Иначе — пропускаем, чтобы сообщение долетело до
    executor.handle_private (сдача результата исполнителем).
    """
    if not message.text or message.text.startswith("/"):
        return False
    user_id = message.from_user.id
    if await cache.get_awaiting_task_count(user_id):
        return True
    if await cache.get_awaiting_reject_reason(user_id):
        return True
    return False


@router.message(_awaiting_moderator_input)
async def handle_task_count_or_reject_reason(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id

    # 1. Проверяем awaiting_task_count
    if await cache.get_awaiting_task_count(user_id):
        await _handle_task_count_input(message)
        return

    # 2. Проверяем awaiting_reject_reason
    reason_key = await cache.get_awaiting_reject_reason(user_id)
    if reason_key:
        await _handle_reject_reason_input(message, bot, reason_key)
        return


async def _handle_task_count_input(message: Message) -> None:
    user_id = message.from_user.id
    text = message.text.strip()

    try:
        count = int(text)
        if count <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Введите корректное число (> 0):")
        return

    await cache.del_awaiting_task_count(user_id)

    # Загружаем задачи из Airtable
    tasks = await airtable.fetch_queue_tasks(count)
    if not tasks:
        await message.answer("Нет задач со статусом 'Очередь' в Airtable.")
        return

    inserted = 0
    for task in tasks:
        record_id = task["id"]
        fields = task.get("fields", {})
        task_number = fields.get("Id")
        task_name = _clean_task_name(fields)
        task_text = fields.get("Для отправки", "")
        mode = fields.get("Режим")
        limit_raw = fields.get("Лимит (час)")
        limit_hours = float(limit_raw) if limit_raw is not None else 0

        ok = await db.insert_task(
            record_id, task_number or 0, task_name, task_text, mode, limit_hours,
        )
        if ok:
            inserted += 1

    await message.answer(f"Загружено задач: {inserted}")

    # Отправляем каждую задачу отдельным сообщением с кнопкой
    loaded = await db.get_tasks_loaded()
    for task in loaded:
        task_preview = (
            f"📋 Задача #{task['task_number']}\n"
            f"{task['task_name']}\n\n"
            f"{task['task_text']}"
        )
        # Карточка после загрузки — решение принимает модератор, поэтому
        # кнопка «Опубликовать» показывается в обоих режимах. В TEST_MODE
        # кнопка «Взять задачу (тест)» появится уже на сообщении, имитирующем
        # публикацию в XX1.3 (см. on_publish).
        await message.answer(task_preview, reply_markup=publish_task_kb(task["record_id"]))


async def _handle_reject_reason_input(message: Message, bot: Bot, reason_key: str) -> None:
    mod_id = message.from_user.id
    reason = message.text.strip()
    await cache.del_awaiting_reject_reason(mod_id)

    parts = reason_key.split(":")
    if len(parts) != 2:
        await message.answer("Ошибка: неверный формат данных.")
        return

    record_id, user_id_str = parts
    user_id = int(user_id_str)

    pending = await db.get_pending_result(user_id)
    if not pending:
        await message.answer("Исполнитель уже не имеет активной задачи.")
        return

    # Отправляем исполнителю в ЛС
    try:
        await bot.send_message(
            user_id,
            f"Результат не принят.\n\n"
            f"Комментарий модератора:\n{reason}\n\n"
            f"Пожалуйста, доработайте и пришлите результат снова.",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить исполнителя %s: %s", user_id, e)

    await message.answer("Причина отправлена исполнителю. Ожидаем доработки.")

    # Редактируем карточку модератора
    mod_msg_id = await cache.get_moderator_message_id(record_id)
    if mod_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=cfg.moderator_group_id,
                message_id=mod_msg_id,
                text=f"↩️ Отправлено на доработку\n\n"
                     f"Задача #{pending['task_number']}: {pending['task_name']}",
            )
        except Exception:
            pass
        await cache.del_moderator_message_id(record_id)


# ── Публикация задачи ─────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("publish_"))
async def on_publish(callback: CallbackQuery, bot: Bot) -> None:
    if not cfg.is_moderator(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return

    record_id = callback.data.split("publish_", 1)[1]
    task = await db.get_task_by_record(record_id)
    if not task or task["status"] != "loaded":
        await callback.answer("Задача не найдена или уже опубликована.", show_alert=True)
        return

    if not await db.is_publishing_enabled():
        await callback.answer("Публикация выключена глобально", show_alert=True)
        return

    chat_id = cfg.target_chat_id()
    prefix = cfg.test_prefix()
    text = prefix + task["task_text"]
    kb = build_publish_keyboard(record_id, cfg.test_mode)

    try:
        msg = await bot.send_message(chat_id, text, parse_mode="MarkdownV2", reply_markup=kb)
    except Exception:
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e2:
            await callback.answer(f"Ошибка отправки: {e2}", show_alert=True)
            return

    updated = await db.update_task_published(record_id, chat_id, msg.message_id)
    if not updated:
        await callback.answer("Не удалось обновить статус.", show_alert=True)
        return

    await cache.del_stale_notified(record_id)

    # Редактируем сообщение в панели
    try:
        now_str = db.utc_now().strftime("%H:%M")
        await callback.message.edit_text(
            f"✅ Задача #{task['task_number']} опубликована в {now_str}",
        )
    except Exception:
        pass

    await callback.answer("Опубликовано!")
    logger.info("[moderator] Задача %s опубликована", record_id)


# ── Взять задачу (TEST_MODE) ──────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("take_test_"))
async def on_take_test(callback: CallbackQuery, bot: Bot) -> None:
    """TEST_MODE: кнопка вместо реакции."""
    if not cfg.test_mode:
        return

    record_id = callback.data.split("take_test_", 1)[1]
    ok = await _assign_user(
        callback.from_user.id,
        callback.from_user.username or callback.from_user.first_name,
        record_id,
        bot,
    )
    if not ok:
        await callback.answer("Задача недоступна", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Задача взята!")


# ── Accept / Reject ────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("accept_"))
async def on_accept(callback: CallbackQuery, bot: Bot) -> None:
    if not cfg.is_moderator(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return

    parts = callback.data.split("accept_")[1].split("_")
    if len(parts) != 2:
        await callback.answer("Неверный формат.", show_alert=True)
        return
    record_id, user_id_str = parts
    user_id = int(user_id_str)

    pending = await db.get_pending_result(user_id)
    task = await db.get_task_by_record(record_id)
    if not pending or not task:
        await callback.answer("Задача не найдена или уже закрыта.", show_alert=True)
        return

    # Читаем результат из Redis; если TTL истёк — берём из PostgreSQL (SR-2).
    result_text = await cache.get_result_text(record_id)
    if not result_text:
        stored = await db.get_pending_result_by_record(record_id)
        result_text = (stored or {}).get("result_content") or ""

    # Обновляем Airtable. Проставляем модератора, который нажал «Принять»
    # (callback.from_user.username) — Airtable делает lookup в «Команда».
    end_time = utc_now_iso()
    moderator_username = callback.from_user.username or callback.from_user.first_name
    await airtable.set_result_done(
        record_id, result_text, end_time, task.get("mode"), moderator_username,
    )

    # Обновляем БД
    await db.update_task_done(record_id)
    await db.delete_pending_result(record_id)
    await cache.del_result_text(record_id)

    # Уведомляем исполнителя
    try:
        await bot.send_message(
            user_id,
            f"Результат принят! Задача #{task['task_number']} завершена. Спасибо!",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить исполнителя %s: %s", user_id, e)

    # Редактируем карточку модератора
    try:
        await callback.message.edit_text(
            f"✅ Принято модератором\n\n"
            f"Задача #{task['task_number']}: {task['task_name']}\n"
            f"Исполнитель: @{pending['username']}",
        )
    except Exception:
        await callback.message.answer("✅ Принято")

    await cache.del_moderator_message_id(record_id)
    await callback.answer("Принято!")
    logger.info("[moderator] Задача %s принята", record_id)


@router.callback_query(lambda c: c.data and c.data.startswith("reject_"))
async def on_reject(callback: CallbackQuery) -> None:
    if not cfg.is_moderator(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return

    parts = callback.data.split("reject_")[1].split("_")
    if len(parts) != 2:
        await callback.answer("Неверный формат.", show_alert=True)
        return
    record_id, user_id_str = parts
    user_id = int(user_id_str)

    # Устанавливаем флаг ожидания причины отказа
    await cache.set_awaiting_reject_reason(callback.from_user.id, record_id, user_id)

    await callback.message.answer(
        "Напишите причину отказа. Она будет отправлена исполнителю:"
    )
    await callback.answer()


# ── Stale notifications ────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("restale_"))
async def on_restale(callback: CallbackQuery, bot: Bot) -> None:
    if not cfg.is_moderator(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return

    if not await db.is_publishing_enabled():
        await callback.answer("Публикация выключена глобально", show_alert=True)
        return

    record_id = callback.data.split("restale_", 1)[1]
    task = await db.get_task_by_record(record_id)
    if not task or task["status"] != "published":
        await callback.answer("Задача уже взята или завершена.", show_alert=True)
        return

    # Удаляем старое сообщение из группы
    try:
        await bot.delete_message(chat_id=task["chat_id"], message_id=task["message_id"])
    except Exception as e:
        logger.warning("Не удалось удалить старое сообщение %s: %s", task["message_id"], e)

    # Публикуем заново
    chat_id = cfg.target_chat_id()
    prefix = cfg.test_prefix()
    text = prefix + task["task_text"]
    kb = build_publish_keyboard(record_id, cfg.test_mode)

    try:
        msg = await bot.send_message(chat_id, text, parse_mode="MarkdownV2", reply_markup=kb)
    except Exception:
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception as e2:
            await callback.answer(f"Ошибка: {e2}", show_alert=True)
            return

    await db.update_task_republish(record_id, msg.message_id)
    # Сбрасываем stale-ключ, чтобы новое сообщение снова попадало под мониторинг.
    await cache.del_stale_notified(record_id)

    # Редактируем уведомление
    try:
        await callback.message.edit_text(
            f"🔁 Опубликовано повторно\n\n"
            f"Задача #{task['task_number']}: {task['task_name']}",
        )
    except Exception:
        pass

    await callback.answer("Опубликовано повторно!")
    logger.info("[moderator] Задача %s переопубликована", record_id)


@router.callback_query(lambda c: c.data and c.data.startswith("skip_stale_"))
async def on_skip_stale(callback: CallbackQuery) -> None:
    if not cfg.is_moderator(callback.from_user.id):
        await callback.answer("Нет прав.", show_alert=True)
        return

    record_id = callback.data.split("skip_stale_", 1)[1]
    task = await db.get_task_by_record(record_id)

    try:
        await callback.message.edit_text(
            f"➡️ Оставлено\n\n"
            f"Задача #{task['task_number'] if task else '?'}",
        )
    except Exception:
        pass
    await callback.answer()


# ── Утилита ────────────────────────────────────────────────────────────────────

async def _assign_user(user_id: int, username: str, record_id: str, bot: Bot) -> bool:
    """Общая логика назначения исполнителя. Возвращает True при успехе."""
    task = await db.get_task_by_record(record_id)
    if not task:
        logger.warning("[assign] Задача %s не найдена в БД (user_id=%s)", record_id, user_id)
        return False

    if await db.has_pending_result(user_id):
        logger.warning(
            "[assign] У user_id=%s уже есть активная задача; отказ по %s",
            user_id, record_id,
        )
        return False

    if task.get("status") != "published":
        logger.warning(
            "[assign] Задача %s имеет статус %r (ожидался 'published'); отказ",
            record_id, task.get("status"),
        )
        return False

    assigned = await db.update_task_assigned(record_id)
    if not assigned:
        logger.warning(
            "[assign] update_task_assigned вернул False для %s (гонка или статус сменился)",
            record_id,
        )
        return False

    await db.insert_pending_result(
        user_id, username, record_id, task.get("task_number"), task.get("task_name"),
    )

    start_time = utc_now_iso()
    await airtable.set_assignee(record_id, username, start_time)

    # Уведомление исполнителю в ЛС
    try:
        await bot.send_message(
            user_id,
            f"Задача #{task['task_number']} взята!\n{task['task_name']}\n\n"
            f"Когда закончите — просто напишите мне результат сюда.\n"
            f"/status — посмотреть статус задачи",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить @%s: %s", username, e)

    # Уведомление в группу модераторов
    try:
        await bot.send_message(
            cfg.moderator_group_id,
            f"👤 @{username} взял(а) задачу #{task['task_number']}: {task['task_name']}",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить модераторов: %s", e)

    logger.info("[reaction] @%s (id=%s) взял задачу %s", username, user_id, record_id)
    return True
