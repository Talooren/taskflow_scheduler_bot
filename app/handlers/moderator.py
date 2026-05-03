"""
Панель модератора и управление задачами.

Команды:
  /панель, /panel — открыть панель
Callbacks:
  load_tasks, clear_queue, refresh_schedule
  publish_{record_id}, take_test_{record_id}, cancel_{record_id}
  accept_{record_id}_{user_id}, reject_{record_id}_{user_id}
  restale_{record_id}, skip_stale_{record_id}
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import airtable, cache, db
from app.config import cfg
from app.keyboards import (
    BTN_CLEAR,
    BTN_INFO,
    BTN_LOAD,
    BTN_REFRESH,
    accept_reject_kb,
    accepted_with_review_kb,
    build_publish_keyboard,
    moderator_reply_kb,
    publish_task_kb,
    published_card_kb,
    stale_notification_kb,
)
from app.utils import utc_now_iso


INFO_TEXT = (
    "ℹ️ <b>Как работает бот и что делает каждая кнопка</b>\n\n"
    "<b>📥 Загрузить задачи</b>\n"
    "Загружает из Airtable записи со статусом «Очередь» (сколько — ты вводишь числом). "
    "На каждую задачу приходит карточка с кнопкой «✅ Опубликовать». "
    "Если задача раньше была завершена/отменена и её вернули в Очередь — она "
    "«перезальётся» заново. Активные (опубликованы или в работе) пропускаются — "
    "бот в ответе показывает, сколько добавлено и сколько пропущено.\n\n"
    "<b>🛑 Очистить очередь</b>\n"
    "Удаляет все загруженные, но ещё не опубликованные задачи. "
    "Задачи, которые уже опубликованы или взяты — не трогаются "
    "(их можно убрать кнопкой «🗑 Отменить» на конкретной карточке).\n\n"
    "<b>🔄 Обновить расписание</b>\n"
    "Подтягивает из Airtable свежие данные для уже загруженных задач "
    "(название, текст, режим, лимит). Удобно, если после загрузки что-то поправили в Airtable.\n\n"
    "<b>✅ Опубликовать</b> (на карточке загруженной задачи)\n"
    "Отправляет задачу в группу ассистентов. В Airtable ставится "
    "Статус=«Опубликована», «Дата публикации»=сейчас, «Группа»=PUBLISH_GROUP_NAME из .env "
    "(по умолчанию «ХХ 1.3»). Карточка обновляется: вместо «Опубликовать» — «🗑 Отменить».\n\n"
    "<b>🗑 Отменить задачу</b> (на карточке опубликованной/взятой задачи)\n"
    "Удаляет сообщение из рабочей группы, в Airtable возвращает Статус=«Очередь» и "
    "стирает «Исполнитель», «Время начала», «Время окончания». Если задачу уже взяли — "
    "исполнителю приходит уведомление в ЛС.\n\n"
    "<b>👤 Взятие задачи</b>\n"
    "Исполнитель берёт задачу <b>реакцией</b> на сообщение в группе (первый поставивший = "
    "исполнитель). В Airtable ставится Статус=«В работе», Время начала, Исполнитель. "
    "Сообщение в группе автоматически редактируется — добавляется метка «✅ Взял @username», "
    "чтобы остальные видели, что задача уже занята.\n\n"
    "<b>📨 Сдача результата</b>\n"
    "Исполнитель открывает /status и жмёт <b>«📝 Сдать результат»</b> — после этого "
    "его сообщения в ЛС копятся в один пакет (текст / фото / файл / голосовое — "
    "сколько надо). После каждой части в ЛС появляются кнопки "
    "<b>«➕ Жду ещё» / «✅ Отправить» / «🗑 Сбросить»</b>. Когда жмёт «Отправить» — "
    "в эту группу прилетает карточка <b>«📨 Результат по задаче #N»</b> с заголовком "
    "и форвардами всех частей подряд. На карточке кнопки:\n"
    "• <b>✅ Принять</b> — Статус → «Завершено», Время окончания, Результат "
    "(склейка всех частей), Модератор = ты.\n"
    "• <b>❌ Не принимать</b> — бот спросит причину, отправит её исполнителю в ЛС; "
    "задача остаётся в работе, исполнитель присылает доработку (накопитель сбрасывается, "
    "ему надо снова нажать «Сдать результат»).\n"
    "• <b>💬 Написать исполнителю</b> — для свободной переписки без отклонения "
    "(в Airtable не пишется). Бот спросит у тебя текст и перешлёт его исполнителю в ЛС.\n"
    "После «Отправить» исполнитель может прислать ещё сообщения — они копятся "
    "в <i>дозалив</i> с кнопкой <b>«📨 Доотправить»</b>. Когда нажмёт — форварды "
    "придут сюда же, как ответ на исходную карточку, и допишутся в результат.\n\n"
    "<b>📋 Проверка ассистентом (двухэтапные задачи)</b>\n"
    "Если у итерации в Airtable выставлено <b>Тип проверки = Проверка ассистентом</b>, "
    "после нажатия <b>«✅ Принять»</b> в карточке остаётся одна кнопка "
    "<b>«📋 Отправить на проверку»</b>. По нажатию бот:\n"
    "• создаёт в «Итерация» новую запись (<b>Режим=Проверка</b>, "
    "<b>Тип проверки=Проверка ассистентом</b>, та же <b>Группа</b> и <b>Задача</b>, "
    "Лимит = REVIEW_TIME_LIMIT_MIN/60 ч). В поле <b>Для отправки</b> бот сам собирает "
    "карточку проверки с правилами, <i>«Не может взять @username»</i>, "
    "<i>«Конечный результат: Заполненная таблица ошибок»</i> и лимитами;\n"
    "• грузит её в локальную БД и шлёт сюда карточку <b>«✅ Опубликовать»</b> — "
    "дальше всё как обычно (Опубликовать → группа исполнителей);\n"
    "• <b>исходный исполнитель</b> сохраняется в <code>excluded_username</code>: "
    "если он попробует взять проверку реакцией — бот ему откажет в ЛС "
    "(«это твоя задача — нельзя»). Остальные исполнители берут как обычно.\n"
    "• <b>проверяющему</b> при взятии бот сразу присылает в ЛС "
    "<i>«📎 Результат проверяемой задачи: …»</i> — это поле «Результат» исходной итерации.\n"
    "Если у исходной <b>Тип проверки = Без проверки</b> или <b>Проверка Заказчиком</b> — "
    "кнопки нет, бот ничего не создаёт, флоу остаётся одноэтапным.\n"
    "Параметры в .env: <code>REVIEW_TIME_LIMIT_MIN</code> (по умолчанию 15 мин — "
    "лимит на саму проверку), <code>REWORK_LIMIT_HOURS</code> (по умолчанию 1 ч — "
    "информационная строка «Лимит доработки» в карточке).\n\n"
    "<b>❓ Вопросы исполнителя</b>\n"
    "В /status у исполнителя есть кнопка <b>«❓ Задать вопрос»</b>. Бот спрашивает "
    "текст, потом просит выбрать «🚧 Блокирующий» / «📨 Не блокирующий» и:\n"
    "• создаёт запись в таблице Airtable <b>«Вопросы»</b> "
    "(линкуется на «Задачи» через Iteration.Задача и на «Исполнители» по Телеграм);\n"
    "• присылает в эту группу карточку с кнопкой <b>«✏️ Ответить»</b>.\n"
    "Жмёшь «Ответить» — бот спрашивает текст, записывает его в поле «Ответ» в Airtable "
    "и шлёт ответ исполнителю в ЛС.\n\n"
    "<b>⏰ Уведомления планировщика</b>\n"
    "• Через 30 минут после публикации без взятия — уведомление с двумя кнопками:\n"
    "   <b>🔁 Опубликовать повторно</b> — <u>старое сообщение удаляется</u> из группы "
    "ассистентов, публикуется новое (с новым отсчётом 30 минут);\n"
    "   <b>➡️ Оставить</b> — ничего не меняется, старое сообщение висит в группе, "
    "новых уведомлений по этой задаче не будет.\n"
    "• Через 15 / 30 / 45 минут, затем 1 / 2 часа после получения результата — "
    "напоминание модератору (если забыл принять или отклонить). После 2 часов "
    "бот замолкает, задача остаётся в pending-списке.\n"
    "• Лимит времени (поле «Лимит (час)» в Airtable):\n"
    "   <b>50%</b> и <b>80%</b> прошло — бот пишет исполнителю в ЛС.\n"
    "   <b>100%</b> (превышение) — бот пишет и исполнителю, и сюда в группу.\n"
    "• Раз в 5 минут — синхронизация с Airtable: если задачу удалили в Airtable вручную, "
    "бот сам отменяет её (удаляет сообщение из группы, ставит «cancelled») и пишет "
    "сюда уведомление.\n\n"
    "<b>👥 Регистрация ассистентов — обязательно!</b>\n"
    "Бот назначает исполнителя только если его Telegram-username есть в таблице "
    "<b>«Исполнители»</b> Airtable (поле <b>Телеграм</b>, с @ или без). Прежде "
    "чем давать новому ассистенту доступ в рабочую группу, <b>заведите его в эту "
    "таблицу</b>. Если незарегистрированный пользователь поставит реакцию на "
    "задачу, она <u>не будет ему назначена</u>: бот напишет ему в ЛС инструкцию, "
    "а сюда прилетит предупреждение с упоминанием username.\n\n"
    "Аналогично для модераторов: <b>доступ к самой этой панели</b> даётся тому, кто есть "
    "в таблице <b>«Команда»</b> Airtable (поле <b>Телеграм</b> = его TG username, с @ или без). "
    "Список модераторов подтягивается из Airtable при старте бота и обновляется "
    "каждые 5 минут — добавил/убрал в таблице, изменения применятся автоматически, "
    "рестарт не нужен. При «✅ Принять» этот же username подставляется в поле "
    "<b>Модератор</b> в Итерации.\n\n"
    "В .env сервера есть ещё <code>MODERATOR_IDS</code> — это break-glass / "
    "owner-доступ по Telegram user_id: такие юзеры остаются модераторами "
    "независимо от Airtable (на случай если Airtable лёг).\n\n"
    "<b>🔐 Про случайных гостей группы</b>\n"
    "Telegram-API не умеет фильтровать, кто ставит реакции — мы компенсируем это "
    "whitelist'ом «Исполнители». Дополнительно: <b>держите рабочую группу закрытой</b> "
    "(invite-only), добавляйте только зарегистрированных.\n\n"
    "<b>/start</b> — это меню.\n"
    "<b>/панель</b> — показать клавиатуру ещё раз (если свернули).\n"
    "<b>/status</b> — показать активную задачу, если ты её брал как исполнитель."
)

logger = logging.getLogger(__name__)
router = Router()


def _strip_md_bold(text: str | None) -> str:
    """Убирает markdown-маркеры `**...**` из текста. Авторы задач в Airtable
    пишут заголовки жирным через `**Название:**`, но у нас отправка plain-text
    (MarkdownV2 ломается на '.', '-', '(', ')' и прочем) — проще выгрести
    звёздочки, чем экранировать всё подряд."""
    if not text:
        return ""
    return text.replace("**", "")


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
    if not cfg.is_moderator(message.from_user):
        await message.answer("Нет прав.")
        return
    await message.answer(
        "Панель модератора. Нажмите кнопку снизу.",
        reply_markup=moderator_reply_kb(),
    )


# ── Text-хэндлеры для кнопок reply-клавиатуры ─────────────────────────────────
# ВАЖНО: эти хэндлеры должны идти ДО _awaiting_moderator_input, иначе ввод
# текста кнопки будет интерпретирован как ответ на запрос «сколько задач?».

@router.message(F.text == BTN_INFO)
async def on_info_text(message: Message) -> None:
    if not cfg.is_moderator(message.from_user):
        return
    await message.answer(INFO_TEXT, parse_mode="HTML")


@router.message(F.text == BTN_LOAD)
async def on_load_text(message: Message) -> None:
    if not cfg.is_moderator(message.from_user):
        return
    await cache.set_awaiting_task_count(message.from_user.id)
    await message.answer("Сколько задач загрузить? Введите число:")


@router.message(F.text == BTN_CLEAR)
async def on_clear_text(message: Message) -> None:
    if not cfg.is_moderator(message.from_user):
        return
    deleted = await db.delete_tasks_loaded()
    await message.answer(
        f"Очередь задач очищена. Удалено задач: {deleted}.\n"
        f"Задачи в работе не затронуты."
    )


@router.message(F.text == BTN_REFRESH)
async def on_refresh_text(message: Message) -> None:
    if not cfg.is_moderator(message.from_user):
        return
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
                    "review_type": fields.get("Тип проверки"),
                },
            )
            refreshed += 1
    await message.answer(f"Расписание обновлено. Обновлено задач: {refreshed}")


@router.callback_query(lambda c: c.data in ("load_tasks", "clear_queue", "refresh_schedule"))
async def on_panel_action(callback: CallbackQuery) -> None:
    if not cfg.is_moderator(callback.from_user):
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
    сейчас вводит ответ на запрос бота (количество задач, причина отказа,
    личное сообщение исполнителю или ответ на вопрос). Иначе — пропускаем,
    чтобы сообщение долетело до executor.handle_private.
    """
    if not message.text or message.text.startswith("/"):
        return False
    user_id = message.from_user.id
    if await cache.get_awaiting_task_count(user_id):
        return True
    if await cache.get_awaiting_reject_reason(user_id):
        return True
    if await cache.get_awaiting_msg_to_user(user_id):
        return True
    if await cache.get_awaiting_question_answer(user_id):
        return True
    return False


@router.message(_awaiting_moderator_input)
async def handle_moderator_input(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id

    if await cache.get_awaiting_task_count(user_id):
        await _handle_task_count_input(message)
        return

    reason_key = await cache.get_awaiting_reject_reason(user_id)
    if reason_key:
        await _handle_reject_reason_input(message, bot, reason_key)
        return

    msg_key = await cache.get_awaiting_msg_to_user(user_id)
    if msg_key:
        await _handle_msg_to_user_input(message, bot, msg_key)
        return

    answer_key = await cache.get_awaiting_question_answer(user_id)
    if answer_key:
        await _handle_question_answer_input(message, bot, answer_key)
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
    updated = 0
    skipped_active: list[str] = []
    new_record_ids: list[str] = []
    for task in tasks:
        record_id = task["id"]
        fields = task.get("fields", {})
        task_number = fields.get("Id")
        task_name = _clean_task_name(fields)
        task_text = fields.get("Для отправки", "")
        mode = fields.get("Режим")
        review_type = fields.get("Тип проверки")
        limit_raw = fields.get("Лимит (час)")
        limit_hours = float(limit_raw) if limit_raw is not None else 0

        result = await db.insert_task(
            record_id, task_number or 0, task_name, task_text, mode, limit_hours,
            review_type=review_type,
        )
        if result == "inserted":
            inserted += 1
            new_record_ids.append(record_id)
        elif result == "updated":
            updated += 1
            new_record_ids.append(record_id)
        elif result == "skipped_active":
            label = f"#{task_number or '?'} {task_name}" if task_name else f"#{task_number or '?'}"
            skipped_active.append(label)

    # Составляем сводное сообщение
    total = inserted + updated
    summary_lines = [f"Запрошено: {len(tasks)} | загружено: {total}"]
    if updated:
        summary_lines.append(f"  • новых: {inserted}, обновлённых: {updated}")
    if skipped_active:
        summary_lines.append(
            f"Пропущено (уже опубликованы / в работе): {len(skipped_active)}"
        )
        for label in skipped_active[:10]:
            summary_lines.append(f"  – {label}")
        if len(skipped_active) > 10:
            summary_lines.append(f"  … и ещё {len(skipped_active) - 10}")
    await message.answer("\n".join(summary_lines))

    # Отправляем карточки только для новых/обновлённых записей этой загрузки
    if new_record_ids:
        loaded = await db.get_tasks_loaded()
        new_set = set(new_record_ids)
        for task in loaded:
            if task["record_id"] not in new_set:
                continue
            task_preview = (
                f"📋 Задача #{task['task_number']}\n"
                f"{_strip_md_bold(task['task_name'])}\n\n"
                f"{_strip_md_bold(task['task_text'])}"
            )
            await message.answer(
                task_preview,
                reply_markup=publish_task_kb(task["record_id"]),
            )


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

    # Сбрасываем накопленный результат и фазу — исполнитель должен начать заново
    # через «📝 Сдать результат». Само pending_results остаётся (задача в работе).
    await cache.clear_result_parts(record_id)
    await cache.clear_addendum_parts(record_id)
    await cache.del_submit_phase(user_id)
    await cache.del_result_text(record_id)


# ── Модератор → исполнитель (личное сообщение) ────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("msgto_"))
async def on_msg_to_user(callback: CallbackQuery) -> None:
    """Кнопка «💬 Написать исполнителю» на карточке результата."""
    if not cfg.is_moderator(callback.from_user):
        await callback.answer("Нет прав.", show_alert=True)
        return
    parts = callback.data.split("msgto_", 1)[1].split("_")
    if len(parts) != 2:
        await callback.answer("Неверный формат.", show_alert=True)
        return
    record_id, user_id_str = parts
    try:
        target_user_id = int(user_id_str)
    except ValueError:
        await callback.answer("Неверный user_id.", show_alert=True)
        return

    await cache.set_awaiting_msg_to_user(callback.from_user.id, target_user_id, record_id)
    await callback.message.answer(
        "💬 Напиши сообщение исполнителю — я перешлю его в ЛС:"
    )
    await callback.answer()


async def _handle_msg_to_user_input(message: Message, bot: Bot, msg_key: str) -> None:
    """Модератор написал текст — пересылаем исполнителю в ЛС."""
    moderator_id = message.from_user.id
    parts = msg_key.split(":")
    if len(parts) != 2:
        await message.answer("Ошибка: неверный формат данных.")
        await cache.del_awaiting_msg_to_user(moderator_id)
        return
    target_user_id_str, record_id = parts
    try:
        target_user_id = int(target_user_id_str)
    except ValueError:
        await message.answer("Ошибка: неверный user_id.")
        await cache.del_awaiting_msg_to_user(moderator_id)
        return

    await cache.del_awaiting_msg_to_user(moderator_id)
    text = message.text.strip()
    moderator_handle = message.from_user.username or message.from_user.first_name or "модератор"

    try:
        await bot.send_message(
            target_user_id,
            f"💬 Сообщение от модератора @{moderator_handle}:\n\n{text}",
        )
        await message.answer("Отправлено.")
    except Exception as e:
        logger.warning("Не удалось отправить сообщение исполнителю %s: %s", target_user_id, e)
        await message.answer(f"Не удалось отправить (исполнитель закрыл ЛС?): {e}")


# ── Модератор → ответ на вопрос ───────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("qans_"))
async def on_answer_question(callback: CallbackQuery) -> None:
    """Кнопка «✏️ Ответить» на карточке вопроса в группе модераторов."""
    if not cfg.is_moderator(callback.from_user):
        await callback.answer("Нет прав.", show_alert=True)
        return
    payload = callback.data.split("qans_", 1)[1]
    parts = payload.split("_")
    if len(parts) != 2:
        await callback.answer("Неверный формат.", show_alert=True)
        return
    question_id, user_id_str = parts
    try:
        executor_user_id = int(user_id_str)
    except ValueError:
        await callback.answer("Неверный user_id.", show_alert=True)
        return

    await cache.set_awaiting_question_answer(
        callback.from_user.id, question_id, executor_user_id,
    )
    await callback.message.answer("✏️ Напиши ответ — пришлю его исполнителю и сохраню в Airtable:")
    await callback.answer()


async def _handle_question_answer_input(message: Message, bot: Bot, answer_key: str) -> None:
    """Текст ответа на вопрос — пишем в Airtable «Вопросы»/Ответ и DM исполнителю."""
    moderator_id = message.from_user.id
    parts = answer_key.split(":")
    if len(parts) != 2:
        await message.answer("Ошибка: неверный формат данных.")
        await cache.del_awaiting_question_answer(moderator_id)
        return
    question_id, executor_user_id_str = parts
    try:
        executor_user_id = int(executor_user_id_str)
    except ValueError:
        await message.answer("Ошибка: неверный user_id.")
        await cache.del_awaiting_question_answer(moderator_id)
        return

    await cache.del_awaiting_question_answer(moderator_id)
    answer_text = message.text.strip()
    moderator_handle = message.from_user.username or message.from_user.first_name or "модератор"

    # 1. Пишем в Airtable
    ok = await airtable.update_question_answer(question_id, answer_text)
    if not ok:
        await message.answer("⚠️ Не удалось записать ответ в Airtable. Сообщение исполнителю всё равно отправляю.")

    # 2. DM исполнителю
    try:
        await bot.send_message(
            executor_user_id,
            f"💬 Ответ модератора @{moderator_handle} на твой вопрос:\n\n{answer_text}",
        )
        await message.answer("Ответ отправлен исполнителю.")
    except Exception as e:
        logger.warning("Не удалось отправить ответ исполнителю %s: %s", executor_user_id, e)
        await message.answer(f"Ответ записан, но исполнителю не доставлено: {e}")


# ── Публикация задачи ─────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("publish_"))
async def on_publish(callback: CallbackQuery, bot: Bot) -> None:
    if not cfg.is_moderator(callback.from_user):
        await callback.answer("Нет прав.", show_alert=True)
        return

    record_id = callback.data.split("publish_", 1)[1]
    task = await db.get_task_by_record(record_id)
    if not task or task["status"] != "loaded":
        await callback.answer("Задача не найдена или уже опубликована.", show_alert=True)
        return

    chat_id = cfg.target_chat_id()
    prefix = cfg.test_prefix()
    text = prefix + _strip_md_bold(task["task_text"])
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

    # Airtable: Статус=Опубликована, Дата публикации=now, Группа=cfg.publish_group_name
    publish_time = utc_now_iso()
    try:
        await airtable.set_published(record_id, publish_time, cfg.publish_group_name)
    except Exception as e:
        logger.warning("[publish] Airtable set_published(%s) failed: %s", record_id, e)

    # Редактируем сообщение в панели и оставляем кнопку «Отменить»
    try:
        now_str = db.utc_now().strftime("%H:%M")
        await callback.message.edit_text(
            f"✅ Задача #{task['task_number']} опубликована в {now_str}",
            reply_markup=published_card_kb(record_id),
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
    if not cfg.is_moderator(callback.from_user):
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
    await cache.del_submit_phase(user_id)
    await cache.clear_result_parts(record_id)
    await cache.clear_addendum_parts(record_id)
    await cache.del_limit_notified_all(record_id)

    # Уведомляем исполнителя
    try:
        await bot.send_message(
            user_id,
            f"Результат принят! Задача #{task['task_number']} завершена. Спасибо!",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить исполнителя %s: %s", user_id, e)

    # Редактируем карточку модератора. Если итерация была Выполнение с
    # Тип проверки=Проверка ассистентом — оставляем одну кнопку «📋 Отправить
    # на проверку», чтобы модератор подтвердил создание Проверка-итерации.
    show_review_btn = (
        task.get("mode") == "Выполнение"
        and task.get("review_type") == "Проверка ассистентом"
    )
    accepted_text = (
        f"✅ Принято модератором\n\n"
        f"Задача #{task['task_number']}: {task['task_name']}\n"
        f"Исполнитель: @{pending['username']}"
    )
    try:
        if show_review_btn:
            await callback.message.edit_text(
                accepted_text + "\n\nТип проверки: Проверка ассистентом",
                reply_markup=accepted_with_review_kb(record_id),
            )
        else:
            await callback.message.edit_text(accepted_text)
    except Exception:
        await callback.message.answer("✅ Принято")

    await cache.del_moderator_message_id(record_id)
    await callback.answer("Принято!")
    logger.info("[moderator] Задача %s принята", record_id)


@router.callback_query(lambda c: c.data and c.data.startswith("send_review_"))
async def on_send_review(callback: CallbackQuery) -> None:
    """«📋 Отправить на проверку» — создаёт в Airtable Проверка-итерацию по
    родительской Выполнение-итерации, грузит её в локальную БД (loaded) и шлёт
    карточку с [✅ Опубликовать] в группу модераторов. Excluded_username =
    исходный исполнитель — он не сможет взять проверку реакцией."""
    if not cfg.is_moderator(callback.from_user):
        await callback.answer("Нет прав.", show_alert=True)
        return

    parent_record_id = callback.data.split("send_review_", 1)[1]
    parent = await db.get_task_by_record(parent_record_id)
    if not parent:
        await callback.answer("Родительская задача не найдена.", show_alert=True)
        return

    if parent.get("mode") != "Выполнение" or parent.get("review_type") != "Проверка ассистентом":
        await callback.answer("Эта задача не требует Проверки ассистентом.", show_alert=True)
        return

    excluded_username = parent.get("assignee_username") or ""
    review = await airtable.create_review_iteration(parent_record_id, excluded_username)
    if not review:
        await callback.answer(
            "Не удалось создать Проверка-итерацию в Airtable. Подробности в логах.",
            show_alert=True,
        )
        return

    new_record_id = review["record_id"]
    new_text = review["task_text"]
    new_name = review["task_name"]
    new_limit = review["limit_hours"]

    # Грузим Проверка-итерацию в локальную БД как loaded.
    # task_number оставляем 0 — он берётся из Airtable Id (autoincrement)
    # только в режиме fetch_queue_tasks; здесь у нас record создан напрямую,
    # без Id — модератор увидит парентский номер в карточке-родителе.
    await db.insert_task(
        new_record_id,
        0,
        new_name,
        new_text,
        "Проверка",
        new_limit,
        review_type="Проверка ассистентом",
        excluded_username=excluded_username,
        parent_record_id=parent_record_id,
    )

    # Шлём в группу модераторов карточку с кнопкой [✅ Опубликовать]
    try:
        await callback.bot.send_message(
            cfg.moderator_group_id,
            f"📋 Создана Проверка-итерация по задаче #{parent.get('task_number')}\n"
            f"{_strip_md_bold(new_name)}\n\n"
            f"Не может взять: @{excluded_username}\n\n"
            f"{_strip_md_bold(new_text)}",
            reply_markup=publish_task_kb(new_record_id),
        )
    except Exception as e:
        logger.error("[send_review] не удалось отправить карточку модераторам: %s", e)
        await callback.answer(
            "Запись в Airtable создана, но карточку отправить не удалось. См. логи.",
            show_alert=True,
        )
        return

    # Убираем кнопку из исходной карточки «Принято»
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.answer("Проверка-итерация создана и отправлена модераторам")
    logger.info(
        "[send_review] parent=%s -> review=%s (excluded=@%s)",
        parent_record_id, new_record_id, excluded_username or "—",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("reject_"))
async def on_reject(callback: CallbackQuery) -> None:
    if not cfg.is_moderator(callback.from_user):
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
    if not cfg.is_moderator(callback.from_user):
        await callback.answer("Нет прав.", show_alert=True)
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
    text = prefix + _strip_md_bold(task["task_text"])
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

    # Обновляем «Дата публикации» в Airtable на новый момент (статус уже Опубликована).
    try:
        await airtable.set_published(record_id, utc_now_iso(), cfg.publish_group_name)
    except Exception as e:
        logger.warning("[restale] Airtable set_published(%s) failed: %s", record_id, e)

    # Редактируем уведомление, добавляем кнопку «Отменить»
    try:
        await callback.message.edit_text(
            f"🔁 Опубликовано повторно\n\n"
            f"Задача #{task['task_number']}: {task['task_name']}",
            reply_markup=published_card_kb(record_id),
        )
    except Exception:
        pass

    await callback.answer("Опубликовано повторно!")
    logger.info("[moderator] Задача %s переопубликована", record_id)


# ── Cancel task ────────────────────────────────────────────────────────────────

@router.callback_query(lambda c: c.data and c.data.startswith("cancel_"))
async def on_cancel(callback: CallbackQuery, bot: Bot) -> None:
    """Отменить задачу. Работает на любом статусе (loaded/published/assigned).
    Удаляет сообщение из рабочей группы, ставит задаче статус='cancelled' в БД,
    откатывает Airtable в «Очередь», уведомляет исполнителя в ЛС, если задача
    уже была взята."""
    if not cfg.is_moderator(callback.from_user):
        await callback.answer("Нет прав.", show_alert=True)
        return

    record_id = callback.data.split("cancel_", 1)[1]
    task = await db.get_task_by_record(record_id)
    if not task:
        await callback.answer("Задача не найдена.", show_alert=True)
        return

    status = task.get("status")
    if status in ("done", "cancelled"):
        await callback.answer("Задача уже закрыта.", show_alert=True)
        return

    # 1. Удалить сообщение из группы (если опубликовано)
    if status in ("published", "assigned") and task.get("chat_id") and task.get("message_id"):
        try:
            await bot.delete_message(
                chat_id=task["chat_id"], message_id=task["message_id"],
            )
        except Exception as e:
            logger.warning(
                "[cancel] не удалось удалить сообщение %s/%s: %s",
                task["chat_id"], task["message_id"], e,
            )

    # 2. Если уже было назначение — уведомить исполнителя и подчистить pending
    if status == "assigned":
        pending = await db.get_pending_result_by_record(record_id)
        if pending:
            target_uid = pending["user_id"]
            try:
                await bot.send_message(
                    target_uid,
                    f"❌ Задача #{task.get('task_number')} «{task.get('task_name')}» "
                    f"отменена модератором. Если ты уже что-то сделал — пришли в ЛС, "
                    f"мы разберёмся.",
                )
            except Exception as e:
                logger.warning(
                    "[cancel] не удалось уведомить исполнителя %s: %s", target_uid, e,
                )
            await db.delete_pending_result(record_id)
            await cache.del_result_text(record_id)
            await cache.del_moderator_message_id(record_id)
            await cache.del_submit_phase(target_uid)

    # 3. Обновить БД и почистить весь кэш по задаче
    await db.update_task_cancelled(record_id)
    await cache.del_stale_notified(record_id)
    await cache.clear_result_parts(record_id)
    await cache.clear_addendum_parts(record_id)
    await cache.del_limit_notified_all(record_id)

    # 4. Откатить Airtable в «Очередь»
    try:
        await airtable.set_status_queue(record_id)
    except Exception as e:
        logger.warning("[cancel] Airtable set_status_queue(%s) failed: %s", record_id, e)

    # 5. Заменить кнопку на текст
    try:
        await callback.message.edit_text(
            f"🗑 Отменено\n\n"
            f"Задача #{task.get('task_number')}: {task.get('task_name')}",
        )
    except Exception:
        pass

    await callback.answer("Задача отменена")
    logger.info("[moderator] Задача %s отменена (был статус: %s)", record_id, status)


@router.callback_query(lambda c: c.data and c.data.startswith("skip_stale_"))
async def on_skip_stale(callback: CallbackQuery) -> None:
    if not cfg.is_moderator(callback.from_user):
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

    # C-group: запрет «проверять собственную работу». excluded_username
    # — оригинальный исполнитель Выполнение-итерации, у Проверка-итерации
    # этот username не может взять задачу. Сравниваем без @ и регистра.
    excl = (task.get("excluded_username") or "").strip().lstrip("@").lower()
    if excl and username and username.strip().lstrip("@").lower() == excl:
        try:
            await bot.send_message(
                user_id,
                f"❌ Эту задачу делал ты сам — проверять её нельзя. "
                f"Пусть возьмёт другой исполнитель.",
            )
        except Exception:
            pass
        logger.info(
            "[assign] @%s — excluded для %s (это его исходная задача)",
            username, record_id,
        )
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

    # Запоминаем username взявшего — пригодится для C-group, чтобы при
    # последующем «Отправить на проверку» автоматически передать его как
    # excluded в Проверка-итерацию.
    await db.set_assignee_username(record_id, username)

    start_time = utc_now_iso()
    await airtable.set_assignee(record_id, username, start_time)

    # Редактируем сообщение в рабочей группе — добавляем метку «✅ Взял @username»
    # и убираем клавиатуру (актуально для TEST_MODE с кнопкой «Взять задачу»;
    # в PROD клавиатуры на сообщении нет, но edit_text всё равно делаем,
    # чтобы остальные ассистенты сразу видели, что задача занята).
    if task.get("chat_id") and task.get("message_id"):
        new_text = (
            f"✅ Задачу взял @{username}\n\n"
            f"{cfg.test_prefix()}{_strip_md_bold(task['task_text'])}"
        )
        try:
            await bot.edit_message_text(
                chat_id=task["chat_id"],
                message_id=task["message_id"],
                text=new_text,
                reply_markup=None,
            )
        except Exception as e:
            logger.warning(
                "[assign] edit_message_text(%s/%s) не удался: %s",
                task["chat_id"], task["message_id"], e,
            )

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

    # C-group: если это Проверка-итерация — DM-им проверяющему результат
    # исходной (Выполнение-)итерации. Источник — Airtable.Результат
    # родителя (parent_record_id). Текст идёт отдельным сообщением, чтобы
    # не смешиваться с инструкциями выше.
    if task.get("mode") == "Проверка" and task.get("parent_record_id"):
        try:
            parent_result = await airtable.fetch_parent_result(task["parent_record_id"])
        except Exception as e:
            logger.warning("[assign] fetch_parent_result(%s) failed: %s", task["parent_record_id"], e)
            parent_result = None

        if parent_result:
            try:
                await bot.send_message(
                    user_id,
                    f"📎 Результат проверяемой задачи:\n\n{parent_result}",
                )
            except Exception as e:
                logger.warning(
                    "[assign] DM parent result to @%s failed: %s", username, e,
                )
        else:
            try:
                await bot.send_message(
                    user_id,
                    "⚠️ Не удалось подгрузить результат исходной задачи. "
                    "Попроси модератора прислать его вручную.",
                )
            except Exception:
                pass

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
