"""
Точка входа TaskFlow Scheduler Bot v2.

Инициализирует PostgreSQL (или SQLite fallback) + Redis (или in-memory),
запускает APScheduler и polling aiogram.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.config import cfg
from app import db
from app import cache
from app.scheduler import setup as setup_scheduler
from app.handlers import router


def _setup_logging() -> None:
    Path("logs").mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/bot.log", encoding="utf-8"),
        ],
    )


async def main() -> None:
    _setup_logging()
    logger = logging.getLogger(__name__)

    # Инициализация БД (PostgreSQL или SQLite fallback)
    await db.init_pool(cfg.pg_dsn)
    await db.ensure_schema()

    # Инициализация кеша (Redis или in-memory fallback)
    await cache.init_client(cfg.redis_url)

    # SR-2: прогреваем Redis незакрытыми результатами из PostgreSQL,
    # чтобы on_accept работал после рестарта даже если Redis пуст.
    warmed = 0
    for row in await db.list_pending_with_result():
        content = row.get("result_content")
        if content:
            await cache.set_result_text(row["record_id"], content)
            warmed += 1
    if warmed:
        logger.info("Прогрет кэш результатов: %s записей", warmed)

    # Bot и Dispatcher
    # parse_mode по дефолту не задаём — многие служебные сообщения содержат
    # символы, зарезервированные в MarkdownV2 (#, ., -, ( ) и т.д.),
    # а те места, где форматирование реально нужно (on_publish / on_restale,
    # cmd_start, cmd_status) указывают parse_mode явно с fallback на plain.
    bot = Bot(token=cfg.bot_token)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Регистрируем роутер хендлеров
    dp.include_router(router)

    # Запускаем планировщик (только check_stale)
    setup_scheduler(bot)

    logger.info(
        "Бот запущен (TEST_MODE=%s, MODERATOR_GROUP_ID=%s, DB=%s, Cache=%s)",
        cfg.test_mode,
        cfg.moderator_group_id,
        "PostgreSQL" if db.is_postgres() else "SQLite",
        "Redis" if cache.is_redis() else "in-memory",
    )

    allowed_updates = [
        "message",
        "callback_query",
        "message_reaction",
        "my_chat_member",
    ]
    try:
        await dp.start_polling(bot, allowed_updates=allowed_updates)
    finally:
        await cache.close_client()
        await db.close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")
