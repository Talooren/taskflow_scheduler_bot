from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app import airtable, db
from app.config import cfg
from app.keyboards import moderator_reply_kb

router = Router()


async def _con_fetch(query: str) -> int:
    pool = db.get_pool()
    async with pool.acquire() as con:
        result = await con.execute(query)
        parts = result.split()
        return int(parts[1]) if len(parts) >= 2 else 0


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    username = message.from_user.username

    # 1. Модератор — приветствие с клавиатурой управления
    if cfg.is_moderator(user_id):
        enabled = await db.is_publishing_enabled()
        await message.answer(
            "🛠 <b>Панель модератора TaskFlow</b>\n\n"
            "Управление публикацией задач — кнопки снизу.\n"
            "Нажми <b>ℹ️ Информация</b>, чтобы посмотреть, что делает каждая кнопка.\n\n"
            "Ещё пригодится:\n"
            "• /панель — показать клавиатуру заново, если свернул.\n"
            "• /status — если брал задачу как исполнитель.",
            parse_mode="HTML",
            reply_markup=moderator_reply_kb(enabled),
        )
        return

    # 2. Исполнитель — проверяем по таблице «Исполнители» (Airtable)
    is_assistant = bool(await airtable.get_assistant_record_id(username))

    if is_assistant:
        await message.answer(
            f"👋 Привет, @{username}!\n\n"
            f"Ты зарегистрирован(а) как исполнитель. Как работать:\n"
            f"• В рабочей группе появляются задачи — поставь <b>реакцию</b> на "
            f"сообщение с задачей, чтобы взять её.\n"
            f"• Когда закончишь — пришли результат в этот чат "
            f"(текстом, файлом, фото — как удобно).\n"
            f"• Модератор примет результат или вернёт с комментарием.\n\n"
            f"/status — показать задачу, которая за тобой сейчас.",
            parse_mode="HTML",
        )
        return

    # 3. Неизвестный — ни модератор, ни в «Исполнители»
    if not username:
        await message.answer(
            "👋 Привет!\n\n"
            "У тебя не установлен username в Telegram — пожалуйста, задай его "
            "в настройках Telegram (Settings → Username) и напиши мне /start заново.\n\n"
            "После этого обратись к модератору, чтобы он зарегистрировал тебя "
            "в таблице «Исполнители».",
        )
    else:
        await message.answer(
            f"👋 Привет!\n\n"
            f"Ты пока не зарегистрирован(а) в системе.\n\n"
            f"Если ты новый ассистент — попроси модератора добавить тебя в таблицу "
            f"«Исполнители» Airtable: поле «Телеграм» = <code>@{username}</code>.\n\n"
            f"После этого сможешь брать задачи в рабочей группе реакцией.",
            parse_mode="HTML",
        )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    pending = await db.get_pending_result(message.from_user.id)
    if pending:
        await message.answer(
            f"Ваша активная задача:\n\n"
            f"<b>#{pending['task_number']} — {pending['task_name']}</b>\n\n"
            f"Когда закончите — отправьте результат мне в этот чат.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "У вас нет активной задачи.\n"
            "Поставьте реакцию на задачу в группе, чтобы взяться за неё."
        )


# ── TEST_MODE команды ──────────────────────────────────────────────────────────

@router.message(Command("test_status"))
async def cmd_test_status(message: Message) -> None:
    if not cfg.test_mode:
        return
    tasks = await db.get_all_active_tasks()
    pending = []
    pool = db.get_pool()
    async with pool.acquire() as con:
        pending = await con.fetch("SELECT * FROM pending_results ORDER BY assigned_at")

    loaded = [t for t in tasks if t["status"] == "loaded"]
    published = [t for t in tasks if t["status"] == "published"]
    assigned = [t for t in tasks if t["status"] == "assigned"]

    lines = ["🧪 TEST STATUS\n"]
    lines.append(f"📋 Загружено задач: {len(tasks)}")
    for t in tasks:
        lines.append(f"   - #{t['task_number']} «{t['task_name']}» [{t['status']}]")

    lines.append(f"\n👤 Активные исполнители: {len(pending)}")
    for p in pending:
        lines.append(f"   - @{p['username']} → задача #{p['task_number']}")

    lines.append(f"\n⚙️ Настройки:")
    lines.append(f"   TEST_MODE: включён")
    lines.append(f"   TEST_USER_ID: {cfg.test_user_id}")

    await message.answer("\n".join(lines))


@router.message(Command("test_reset"))
async def cmd_test_reset(message: Message) -> None:
    if not cfg.test_mode:
        return
    import app.cache as cache
    deleted_tasks = await db.delete_all_tasks()
    deleted_pending = await _con_fetch("DELETE FROM pending_results")
    deleted_redis = await cache.clear_all_test_data()

    await message.answer(
        f"🧹 Тестовые данные очищены.\n"
        f"Задач удалено: {deleted_tasks}\n"
        f"Исполнителей удалено: {deleted_pending}\n"
        f"Redis ключей удалено: {deleted_redis}"
    )


@router.message(Command("test_skip_stale"))
async def cmd_test_skip_stale(message: Message) -> None:
    """Принудительно запустить check_stale, игнорируя 30-минутный порог."""
    if not cfg.test_mode:
        return
    from app.scheduler import check_stale_force
    await message.answer("⏰ Запуск проверки простоя...")
    await check_stale_force()
    await message.answer("✅ Проверка завершена.")
