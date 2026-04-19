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

        # Модераторы
        _ids = _opt("MODERATOR_IDS", "")
        self.moderator_ids: set[int] = {
            int(x.strip()) for x in _ids.split(",") if x.strip().isdigit()
        }

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

    def is_moderator(self, user_id: int) -> bool:
        return user_id in self.moderator_ids

    def target_chat_id(self) -> int:
        """Куда публиковать задачи."""
        if self.test_mode:
            return self.moderator_group_id
        return self.group_id

    def test_prefix(self) -> str:
        return "[🧪 TEST MODE] " if self.test_mode else ""


cfg = Config()
