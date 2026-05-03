from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _opt(name: str, default: str = "") -> str:
    return os.getenv(name, default)


class Config:
    def __init__(self) -> None:
        # Telegram
        self.bot_token: str = _require("BOT_TOKEN")

        # Airtable
        self.airtable_api_key: str = _require("AIRTABLE_API_KEY")
        self.airtable_base_id: str = _require("AIRTABLE_BASE_ID")
        self.airtable_table: str = _opt("AIRTABLE_TABLE", "Итерация")

        # Telegram группы
        self.group_id: int = int(_require("GROUP_ID"))
        self.moderator_group_id: int = int(_require("MODERATOR_GROUP_ID"))

        # Имя группы для записи в Airtable (поле «Группа» в «Итерация»).
        # Декаплено от group_id: позволяет публиковать в тестовый чат, но
        # фиксировать в Airtable «ХХ 1.3» как реальную рабочую группу.
        self.publish_group_name: str = _opt("PUBLISH_GROUP_NAME", "ХХ 1.3")

        # Модераторы: основной источник — таблица «Команда» Airtable
        # (username'ы подтягиваются в cfg.moderator_usernames при старте
        # и обновляются раз в 5 минут планировщиком — см. run.py и
        # app/scheduler.py). MODERATOR_IDS в .env остаётся как
        # break-glass fallback: пользователи из него всегда модераторы,
        # даже если Airtable недоступен.
        _ids = _opt("MODERATOR_IDS", "")
        self.moderator_ids: set[int] = {
            int(x.strip()) for x in _ids.split(",") if x.strip().isdigit()
        }
        self.moderator_usernames: set[str] = set()  # пополняется async из Airtable

        # PostgreSQL (опционально — при пустом значении используется SQLite)
        self.pg_dsn: str = _opt("PG_DSN", "")

        # Redis (опционально — при пустом значении используется in-memory)
        self.redis_url: str = _opt("REDIS_URL", "")

        # Тестовый режим
        self.test_mode: bool = _opt("TEST_MODE", "false").lower() == "true"
        self.test_user_id: int = int(_opt("TEST_USER_ID", "0"))

        if self.test_mode:
            logger.warning(
                "⚠️  TEST MODE enabled — TEST_USER_ID=%s, публикации → MODERATOR_GROUP_ID",
                self.test_user_id,
            )
            # В TEST_MODE тестировщик — тоже модератор
            if self.test_user_id:
                self.moderator_ids.add(self.test_user_id)

    def is_moderator(self, user) -> bool:
        """Проверка прав модератора. Принимает aiogram-User (обычно
        `message.from_user` или `callback.from_user`). Модератором считается:
        1) user.id из MODERATOR_IDS (.env, break-glass owner-доступ), ИЛИ
        2) user.username из кэша cfg.moderator_usernames, который наполняется
           из таблицы «Команда» Airtable (нормализация: lowercase, без @)."""
        if user is None:
            return False
        # break-glass: список id в .env
        uid = getattr(user, "id", None)
        if uid is not None and uid in self.moderator_ids:
            return True
        # основной путь: username в таблице «Команда»
        uname = getattr(user, "username", None)
        if uname:
            normalized = uname.strip().lstrip("@").lower()
            if normalized in self.moderator_usernames:
                return True
        return False

    def target_chat_id(self) -> int:
        """Куда публиковать задачи."""
        if self.test_mode:
            return self.moderator_group_id
        return self.group_id

    def test_prefix(self) -> str:
        return "[🧪 TEST MODE] " if self.test_mode else ""


cfg = Config()
