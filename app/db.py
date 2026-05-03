"""
Хранилище данных: PostgreSQL (основной) или SQLite (fallback для тестов).

При пустом PG_DSN автоматически используется SQLite.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("data/state.db")

# ── Режим работы ───────────────────────────────────────────────────────────────
_use_postgres: bool = False
_pg_pool = None  # asyncpg.Pool | None
_sqlite_lock = None


def is_postgres() -> bool:
    return _use_postgres


async def init_pool(dsn: str | None = None) -> None:
    """Инициализировать БД. Если dsn пустой — SQLite fallback."""
    global _use_postgres, _pg_pool

    if not dsn or not dsn.strip():
        _use_postgres = False
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        logger.info("SQLite fallback mode: %s", DB_PATH.resolve())
        # Создаём схему
        _sqlite_init_schema()
        return

    import asyncpg
    _use_postgres = True
    _pg_pool = await asyncpg.create_pool(dsn=dsn)
    logger.info("PostgreSQL pool initialized: %s", dsn.split("@")[-1] if "@" in dsn else dsn)


async def init_pool_from_obj(pool_obj) -> None:
    """Инициализировать уже созданный pool (для тестов)."""
    global _use_postgres, _pg_pool
    _use_postgres = True
    _pg_pool = pool_obj


async def close_pool() -> None:
    global _use_postgres, _pg_pool
    if _pg_pool:
        await _pg_pool.close()
        logger.info("PostgreSQL pool closed")
        _pg_pool = None
        _use_postgres = False


def get_pool():
    """Вернуть инициализированный asyncpg pool.

    Бросает RuntimeError, если init_pool ещё не отработал или БД работает
    в SQLite-режиме (в этом случае прямой доступ к пулу бессмыслен —
    надо пользоваться функциями-обёртками из этого модуля).
    """
    if not _use_postgres:
        raise RuntimeError(
            "db.get_pool() недоступен в SQLite-режиме. "
            "Либо используйте функции-обёртки db.*, либо задайте PG_DSN."
        )
    if _pg_pool is None:
        raise RuntimeError("db.get_pool() вызван до init_pool().")
    return _pg_pool


def _get_sqlite_conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _sqlite_init_schema() -> None:
    con = _get_sqlite_conn()
    try:
        con.executescript(_SCHEMA_SQLITE)
        logger.info("SQLite schema ensured")
    finally:
        con.close()


def _sqlite_migrate_result_columns() -> None:
    """Добавляет result_* колонки в старые БД (до SR-2). SQLite не умеет
    ADD COLUMN IF NOT EXISTS, поэтому проверяем через PRAGMA."""
    con = _get_sqlite_conn()
    try:
        cols = {row["name"] for row in con.execute("PRAGMA table_info(pending_results)").fetchall()}
        if "result_content" not in cols:
            con.execute("ALTER TABLE pending_results ADD COLUMN result_content TEXT")
        if "result_type" not in cols:
            con.execute("ALTER TABLE pending_results ADD COLUMN result_type TEXT")
        if "result_received_at" not in cols:
            con.execute("ALTER TABLE pending_results ADD COLUMN result_received_at TEXT")
        con.commit()
    finally:
        con.close()


def _dict_row(row, keys) -> dict:
    """Преобразовать строку результата в dict."""
    if row is None:
        return None
    return dict(zip(keys, row))


# ── Схема SQLite ───────────────────────────────────────────────────────────────

_SCHEMA_SQLITE = """
    CREATE TABLE IF NOT EXISTS tasks (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id      TEXT    NOT NULL UNIQUE,
        task_number    INTEGER,
        task_name      TEXT    NOT NULL,
        task_text      TEXT    NOT NULL,
        mode           TEXT,
        limit_hours    FLOAT   DEFAULT 0,
        status         TEXT    NOT NULL DEFAULT 'loaded',
        chat_id        INTEGER,
        message_id     INTEGER,
        published_at   TEXT,
        loaded_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS pending_results (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id            INTEGER  NOT NULL UNIQUE,
        username           TEXT,
        record_id          TEXT    NOT NULL,
        task_number        INTEGER,
        task_name          TEXT,
        assigned_at        TEXT DEFAULT (datetime('now')),
        result_content     TEXT,
        result_type        TEXT,
        result_received_at TEXT
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""


async def ensure_schema() -> None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(_SCHEMA_PG)
            # Миграция для БД, созданных до SR-2: добавляем колонки результата,
            # если их ещё нет. IF NOT EXISTS безопасно для ре-запуска.
            await con.execute(
                "ALTER TABLE pending_results "
                "ADD COLUMN IF NOT EXISTS result_content TEXT"
            )
            await con.execute(
                "ALTER TABLE pending_results "
                "ADD COLUMN IF NOT EXISTS result_type TEXT"
            )
            await con.execute(
                "ALTER TABLE pending_results "
                "ADD COLUMN IF NOT EXISTS result_received_at TIMESTAMPTZ"
            )
        logger.info("PostgreSQL schema ensured")
    else:
        _sqlite_init_schema()
        _sqlite_migrate_result_columns()


_SCHEMA_PG = """
    CREATE TABLE IF NOT EXISTS tasks (
        id             SERIAL PRIMARY KEY,
        record_id      TEXT    NOT NULL UNIQUE,
        task_number    INTEGER,
        task_name      TEXT    NOT NULL,
        task_text      TEXT    NOT NULL,
        mode           TEXT,
        limit_hours    FLOAT   DEFAULT 0,
        status         TEXT    NOT NULL DEFAULT 'loaded',
        chat_id        BIGINT,
        message_id     BIGINT,
        published_at   TIMESTAMPTZ,
        loaded_at      TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS pending_results (
        id                 SERIAL PRIMARY KEY,
        user_id            BIGINT  NOT NULL UNIQUE,
        username           TEXT,
        record_id          TEXT    NOT NULL,
        task_number        INTEGER,
        task_name          TEXT,
        assigned_at        TIMESTAMPTZ DEFAULT NOW(),
        result_content     TEXT,
        result_type        TEXT,
        result_received_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""


# ── CRUD: tasks ────────────────────────────────────────────────────────────────

async def insert_task(
    record_id: str,
    task_number: int,
    task_name: str,
    task_text: str,
    mode: str | None,
    limit_hours: float,
) -> str:
    """UPSERT задачи. Возвращает код результата:
        'inserted'       — новая запись добавлена;
        'updated'        — существовала в 'loaded'/'done', сброшена в 'loaded'
                           с обновлёнными полями (кейс: Airtable вернул задачу
                           в Очередь после закрытия или редактирования);
        'skipped_active' — существует в 'published' или 'assigned', не тронута,
                           чтобы не сломать активную работу.

    Дополнительно учитываем 'cancelled' — отменённые задачи можно пере-загрузить
    как 'loaded' (модератор отменил, потом передумал и заново положил в очередь).
    """
    existing = await get_task_by_record(record_id)
    if existing and existing.get("status") in ("published", "assigned"):
        return "skipped_active"

    is_update = existing is not None
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO tasks (record_id, task_number, task_name, task_text,
                                   mode, limit_hours, status, loaded_at)
                VALUES ($1, $2, $3, $4, $5, $6, 'loaded', NOW())
                ON CONFLICT (record_id) DO UPDATE SET
                    task_number  = EXCLUDED.task_number,
                    task_name    = EXCLUDED.task_name,
                    task_text    = EXCLUDED.task_text,
                    mode         = EXCLUDED.mode,
                    limit_hours  = EXCLUDED.limit_hours,
                    status       = 'loaded',
                    chat_id      = NULL,
                    message_id   = NULL,
                    published_at = NULL,
                    loaded_at    = NOW()
                """,
                record_id, task_number, task_name, task_text, mode, limit_hours,
            )
    else:
        con = _get_sqlite_conn()
        try:
            con.execute(
                """
                INSERT INTO tasks (record_id, task_number, task_name, task_text,
                                   mode, limit_hours, status, loaded_at)
                VALUES (?, ?, ?, ?, ?, ?, 'loaded', datetime('now'))
                ON CONFLICT(record_id) DO UPDATE SET
                    task_number  = excluded.task_number,
                    task_name    = excluded.task_name,
                    task_text    = excluded.task_text,
                    mode         = excluded.mode,
                    limit_hours  = excluded.limit_hours,
                    status       = 'loaded',
                    chat_id      = NULL,
                    message_id   = NULL,
                    published_at = NULL,
                    loaded_at    = datetime('now')
                """,
                (record_id, task_number, task_name, task_text, mode, limit_hours),
            )
            con.commit()
        finally:
            con.close()
    return "updated" if is_update else "inserted"


async def get_tasks_loaded() -> list[dict]:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM tasks WHERE status = 'loaded' ORDER BY loaded_at"
            )
            return [dict(r) for r in rows]
    else:
        con = _get_sqlite_conn()
        try:
            rows = con.execute(
                "SELECT * FROM tasks WHERE status = 'loaded' ORDER BY loaded_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


async def get_task_by_record(record_id: str) -> dict | None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM tasks WHERE record_id = $1", record_id
            )
            return dict(row) if row else None
    else:
        con = _get_sqlite_conn()
        try:
            row = con.execute(
                "SELECT * FROM tasks WHERE record_id = ?", (record_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


async def get_task_by_message(chat_id: int, message_id: int) -> dict | None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM tasks WHERE chat_id = $1 AND message_id = $2 AND status = 'published'",
                chat_id, message_id,
            )
            return dict(row) if row else None
    else:
        con = _get_sqlite_conn()
        try:
            row = con.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND message_id = ? AND status = 'published'",
                (chat_id, message_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


async def update_task_published(
    record_id: str,
    chat_id: int,
    message_id: int,
) -> bool:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            result = await con.execute(
                """
                UPDATE tasks
                SET status = 'published', chat_id = $1, message_id = $2, published_at = NOW()
                WHERE record_id = $3 AND status = 'loaded'
                """,
                chat_id, message_id, record_id,
            )
            return "UPDATE" in result
    else:
        con = _get_sqlite_conn()
        try:
            cur = con.execute(
                """
                UPDATE tasks
                SET status = 'published', chat_id = ?, message_id = ?, published_at = ?
                WHERE record_id = ? AND status = 'loaded'
                """,
                (chat_id, message_id, _now_iso(), record_id),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


async def update_task_assigned(record_id: str) -> bool:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            result = await con.execute(
                """
                UPDATE tasks SET status = 'assigned'
                WHERE record_id = $1 AND status = 'published'
                RETURNING record_id
                """,
                record_id,
            )
            return "UPDATE 1" in result
    else:
        con = _get_sqlite_conn()
        try:
            cur = con.execute(
                """
                UPDATE tasks SET status = 'assigned'
                WHERE record_id = ? AND status = 'published'
                """,
                (record_id,),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


async def update_task_done(record_id: str) -> None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(
                "UPDATE tasks SET status = 'done' WHERE record_id = $1", record_id
            )
    else:
        con = _get_sqlite_conn()
        try:
            con.execute(
                "UPDATE tasks SET status = 'done' WHERE record_id = ?", (record_id,)
            )
            con.commit()
        finally:
            con.close()


async def update_task_cancelled(record_id: str) -> None:
    """Отменить задачу: status='cancelled'. Не удаляет строку, чтобы при
    повторной загрузке из Airtable можно было увидеть историю и решить, что
    делать. insert_task поверх 'cancelled' сбрасывает обратно в 'loaded'."""
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(
                "UPDATE tasks SET status = 'cancelled' WHERE record_id = $1", record_id
            )
    else:
        con = _get_sqlite_conn()
        try:
            con.execute(
                "UPDATE tasks SET status = 'cancelled' WHERE record_id = ?", (record_id,)
            )
            con.commit()
        finally:
            con.close()


async def delete_tasks_loaded() -> int:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            result = await con.execute("DELETE FROM tasks WHERE status = 'loaded'")
            parts = result.split()
            return int(parts[1]) if len(parts) >= 2 else 0
    else:
        con = _get_sqlite_conn()
        try:
            cur = con.execute("DELETE FROM tasks WHERE status = 'loaded'")
            con.commit()
            return cur.rowcount
        finally:
            con.close()


async def delete_all_tasks() -> int:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            result = await con.execute("DELETE FROM tasks")
            parts = result.split()
            return int(parts[1]) if len(parts) >= 2 else 0
    else:
        con = _get_sqlite_conn()
        try:
            cur = con.execute("DELETE FROM tasks")
            con.commit()
            return cur.rowcount
        finally:
            con.close()


async def get_stale_published(minutes: int) -> list[dict]:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT * FROM tasks
                WHERE status = 'published'
                  AND published_at < NOW() - ($1 * INTERVAL '1 minute')
                ORDER BY published_at
                """,
                minutes,
            )
            return [dict(r) for r in rows]
    else:
        con = _get_sqlite_conn()
        try:
            # SQLite: published_at < datetime('now', '-N minutes')
            rows = con.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'published'
                  AND published_at < datetime('now', ? || ' minutes')
                ORDER BY published_at
                """,
                (f"-{minutes}",),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


async def update_task_republish(record_id: str, message_id: int) -> None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(
                """
                UPDATE tasks SET message_id = $1, published_at = NOW()
                WHERE record_id = $2
                """,
                message_id, record_id,
            )
    else:
        con = _get_sqlite_conn()
        try:
            con.execute(
                """
                UPDATE tasks SET message_id = ?, published_at = ?
                WHERE record_id = ?
                """,
                (message_id, _now_iso(), record_id),
            )
            con.commit()
        finally:
            con.close()


async def get_all_active_tasks() -> list[dict]:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM tasks WHERE status != 'done' ORDER BY loaded_at"
            )
            return [dict(r) for r in rows]
    else:
        con = _get_sqlite_conn()
        try:
            rows = con.execute(
                "SELECT * FROM tasks WHERE status != 'done' ORDER BY loaded_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


async def update_task_fields(record_id: str, fields: dict) -> None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(
                """
                UPDATE tasks SET
                    task_name = COALESCE($2, task_name),
                    task_text = COALESCE($3, task_text),
                    mode = COALESCE($4, mode),
                    limit_hours = COALESCE($5, limit_hours)
                WHERE record_id = $1
                """,
                record_id,
                fields.get("task_name"),
                fields.get("task_text"),
                fields.get("mode"),
                fields.get("limit_hours"),
            )
    else:
        con = _get_sqlite_conn()
        try:
            row = con.execute("SELECT * FROM tasks WHERE record_id = ?", (record_id,)).fetchone()
            if row:
                con.execute(
                    """
                    UPDATE tasks SET
                        task_name = ?,
                        task_text = ?,
                        mode = ?,
                        limit_hours = ?
                    WHERE record_id = ?
                    """,
                    (
                        fields.get("task_name") or row["task_name"],
                        fields.get("task_text") or row["task_text"],
                        fields.get("mode") or row["mode"],
                        fields.get("limit_hours") or row["limit_hours"],
                        record_id,
                    ),
                )
                con.commit()
        finally:
            con.close()


# ── CRUD: pending_results ──────────────────────────────────────────────────────

async def has_pending_result(user_id: int) -> bool:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            row = await con.fetchval(
                "SELECT 1 FROM pending_results WHERE user_id = $1", user_id
            )
            return row is not None
    else:
        con = _get_sqlite_conn()
        try:
            row = con.execute(
                "SELECT id FROM pending_results WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row is not None
        finally:
            con.close()


async def insert_pending_result(
    user_id: int,
    username: str,
    record_id: str,
    task_number: int | None,
    task_name: str,
) -> None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO pending_results (user_id, username, record_id, task_number, task_name)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE SET
                    record_id = EXCLUDED.record_id,
                    task_number = EXCLUDED.task_number,
                    task_name = EXCLUDED.task_name,
                    assigned_at = NOW()
                """,
                user_id, username, record_id, task_number, task_name,
            )
    else:
        con = _get_sqlite_conn()
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO pending_results
                    (user_id, username, record_id, task_number, task_name, assigned_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, record_id, task_number, task_name, _now_iso()),
            )
            con.commit()
        finally:
            con.close()


async def get_pending_result(user_id: int) -> dict | None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM pending_results WHERE user_id = $1", user_id
            )
            return dict(row) if row else None
    else:
        con = _get_sqlite_conn()
        try:
            row = con.execute(
                "SELECT * FROM pending_results WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


async def get_pending_result_by_record(record_id: str) -> dict | None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT * FROM pending_results WHERE record_id = $1", record_id
            )
            return dict(row) if row else None
    else:
        con = _get_sqlite_conn()
        try:
            row = con.execute(
                "SELECT * FROM pending_results WHERE record_id = ?", (record_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


async def set_pending_result_content(
    record_id: str,
    content: str,
    result_type: str,
) -> None:
    """Сохраняет результат в PG/SQLite. Источник правды для результата —
    Redis остаётся кэшем. См. SR-2."""
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(
                """
                UPDATE pending_results
                SET result_content = $1,
                    result_type = $2,
                    result_received_at = NOW()
                WHERE record_id = $3
                """,
                content, result_type, record_id,
            )
    else:
        con = _get_sqlite_conn()
        try:
            con.execute(
                """
                UPDATE pending_results
                SET result_content = ?,
                    result_type = ?,
                    result_received_at = ?
                WHERE record_id = ?
                """,
                (content, result_type, _now_iso(), record_id),
            )
            con.commit()
        finally:
            con.close()


async def list_assigned_for_limit_check() -> list[dict]:
    """Все назначенные задачи с заполненным limit_hours, у которых исполнитель
    ещё не отправил результат. Используется шедулером check_limits для
    напоминаний о превышении 50/80/100% лимита.
    Возвращает: record_id, task_number, task_name, limit_hours,
                user_id, username, assigned_at.
    """
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            rows = await con.fetch(
                """
                SELECT t.record_id, t.task_number, t.task_name, t.limit_hours,
                       p.user_id, p.username, p.assigned_at
                FROM tasks t
                JOIN pending_results p ON t.record_id = p.record_id
                WHERE t.status = 'assigned'
                  AND t.limit_hours IS NOT NULL AND t.limit_hours > 0
                  AND p.result_content IS NULL
                """
            )
            return [dict(r) for r in rows]
    else:
        con = _get_sqlite_conn()
        try:
            rows = con.execute(
                """
                SELECT t.record_id, t.task_number, t.task_name, t.limit_hours,
                       p.user_id, p.username, p.assigned_at
                FROM tasks t
                JOIN pending_results p ON t.record_id = p.record_id
                WHERE t.status = 'assigned'
                  AND t.limit_hours IS NOT NULL AND t.limit_hours > 0
                  AND p.result_content IS NULL
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


async def list_pending_with_result() -> list[dict]:
    """Все pending_results с уже полученным, но ещё не принятым результатом.
    Используется при старте бота для прогрева Redis (SR-2) и для напоминаний
    модератору (SR-3)."""
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM pending_results "
                "WHERE result_content IS NOT NULL AND result_received_at IS NOT NULL"
            )
            return [dict(r) for r in rows]
    else:
        con = _get_sqlite_conn()
        try:
            rows = con.execute(
                "SELECT * FROM pending_results "
                "WHERE result_content IS NOT NULL AND result_received_at IS NOT NULL"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


async def delete_pending_result(record_id: str) -> None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(
                "DELETE FROM pending_results WHERE record_id = $1", record_id
            )
    else:
        con = _get_sqlite_conn()
        try:
            con.execute(
                "DELETE FROM pending_results WHERE record_id = ?", (record_id,)
            )
            con.commit()
        finally:
            con.close()


async def delete_pending_result_by_user(user_id: int) -> int:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            result = await con.execute(
                "DELETE FROM pending_results WHERE user_id = $1", user_id
            )
            parts = result.split()
            return int(parts[1]) if len(parts) >= 2 else 0
    else:
        con = _get_sqlite_conn()
        try:
            cur = con.execute(
                "DELETE FROM pending_results WHERE user_id = ?", (user_id,)
            )
            con.commit()
            return cur.rowcount
        finally:
            con.close()


# ── CRUD: settings ─────────────────────────────────────────────────────────────

async def get_setting(key: str, default: str = "") -> str:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            row = await con.fetchval(
                "SELECT value FROM settings WHERE key = $1", key
            )
            return row if row is not None else default
    else:
        con = _get_sqlite_conn()
        try:
            row = con.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default
        finally:
            con.close()


async def set_setting(key: str, value: str) -> None:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            await con.execute(
                """
                INSERT INTO settings (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                key, value,
            )
    else:
        con = _get_sqlite_conn()
        try:
            con.execute(
                """
                INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)
                """,
                (key, value),
            )
            con.commit()
        finally:
            con.close()


PUBLISHING_KEY = "publishing_enabled"


async def is_publishing_enabled() -> bool:
    val = await get_setting(PUBLISHING_KEY, "0")
    return val == "1"


async def set_publishing(enabled: bool) -> None:
    await set_setting(PUBLISHING_KEY, "1" if enabled else "0")


async def get_all_settings() -> dict[str, str]:
    if _use_postgres:
        async with _pg_pool.acquire() as con:
            rows = await con.fetch("SELECT key, value FROM settings")
            return {r["key"]: r["value"] for r in rows}
    else:
        con = _get_sqlite_conn()
        try:
            rows = con.execute("SELECT key, value FROM settings").fetchall()
            return {r["key"]: r["value"] for r in rows}
        finally:
            con.close()


# ── Утилиты ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
