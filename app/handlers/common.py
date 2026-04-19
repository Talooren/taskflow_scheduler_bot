from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app import db
from app.config import cfg

router = Router()


async def _con_fetch(query: str) -> int:
    pool = db.get_pool()
    async with pool.acquire() as con:
        result = await con.execute(query)
        parts = result.split()
        return int(parts[1]) if len(parts) >= 2 else 0


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    mod_hint = (
        "\n\nВы модератор: /панель — управление публикацией"
        if cfg.is_moderator(message.from_user.id)
        else ""
    )
    await message.answer(
        f"<b>TaskFlow Bot</b>\n\n"
        f"Бот публикует задачи в группу по расписанию.\n\n"
        f"• Поставьте реакцию на задачу в группе, чтобы взять её.\n"
        f"• Результат отправьте мне в личные сообщения.\n\n"
        f"/status — посмотреть текущую задачу"
        f"{mod_hint}",
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
