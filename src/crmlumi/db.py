"""Локальное состояние: последний виденный снимок каждого заказа.

Единственное место в проекте, знающее схему SQLite.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import Order, Shoot

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_cache (
    order_id TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    joined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS role_assignments (
    role_name TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    PRIMARY KEY (role_name, chat_id)
);

CREATE TABLE IF NOT EXISTS claim_locks (
    order_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    claimed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_broadcasts (
    order_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (order_id, chat_id)
);

CREATE TABLE IF NOT EXISTS ms_accounts (
    chat_id INTEGER PRIMARY KEY,
    ms_user_id TEXT NOT NULL,
    ms_email TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks_created (
    order_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    list_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (order_id, role_name)
);

CREATE TABLE IF NOT EXISTS tasks_completed (
    order_id TEXT NOT NULL,
    role_name TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (order_id, role_name)
);

CREATE TABLE IF NOT EXISTS status_history (
    order_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    since TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shoot_cache (
    shoot_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    snapshot TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shoot_claims (
    shoot_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    claimed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shoot_tasks (
    shoot_id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    signature TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    chat_id INTEGER PRIMARY KEY,
    reminder_hours INTEGER
);

CREATE TABLE IF NOT EXISTS reminder_log (
    chat_id INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, order_id, status)
);

CREATE TABLE IF NOT EXISTS sent_notices (
    notice_key TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_check_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_run_date TEXT
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _connection(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() ещё не вызван"
        return self._conn

    async def get_cached_order(self, order_id: str) -> Order | None:
        cursor = await self._connection.execute(
            "SELECT snapshot FROM order_cache WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return Order(**json.loads(row[0]))

    async def save_order(self, order: Order) -> None:
        await self._connection.execute(
            """
            INSERT INTO order_cache (order_id, snapshot, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                snapshot = excluded.snapshot,
                updated_at = excluded.updated_at
            """,
            (
                order.order_id,
                json.dumps(asdict(order), ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._connection.commit()

    async def register_user(
        self, chat_id: int, *, username: str | None, display_name: str | None
    ) -> bool:
        """Регистрирует пользователя. Возвращает True, если он новый."""
        cursor = await self._connection.execute(
            "SELECT 1 FROM users WHERE chat_id = ?", (chat_id,)
        )
        existing = await cursor.fetchone()
        if existing is not None:
            await self._connection.execute(
                "UPDATE users SET username = ?, display_name = ? WHERE chat_id = ?",
                (username, display_name, chat_id),
            )
            await self._connection.commit()
            return False

        await self._connection.execute(
            "INSERT INTO users (chat_id, username, display_name, joined_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, username, display_name, datetime.now(timezone.utc).isoformat()),
        )
        await self._connection.commit()
        return True

    async def list_users(self) -> list[dict]:
        cursor = await self._connection.execute(
            "SELECT chat_id, username, display_name FROM users ORDER BY joined_at"
        )
        rows = await cursor.fetchall()
        return [
            {"chat_id": row[0], "username": row[1], "display_name": row[2]}
            for row in rows
        ]

    async def get_role_assignees(self, role_name: str) -> list[int]:
        cursor = await self._connection.execute(
            "SELECT chat_id FROM role_assignments WHERE role_name = ?", (role_name,)
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def set_role_assignee(self, role_name: str, chat_id: int) -> None:
        """Заменяет исполнителя роли одним человеком (для ролей-одиночек)."""
        await self._connection.execute(
            "DELETE FROM role_assignments WHERE role_name = ?", (role_name,)
        )
        await self._connection.execute(
            "INSERT INTO role_assignments (role_name, chat_id) VALUES (?, ?)",
            (role_name, chat_id),
        )
        await self._connection.commit()

    async def toggle_role_member(self, role_name: str, chat_id: int) -> bool:
        """Для ролей с несколькими исполнителями: добавляет/убирает одного.

        Возвращает True, если человек теперь в роли, False — если убран.
        """
        cursor = await self._connection.execute(
            "SELECT 1 FROM role_assignments WHERE role_name = ? AND chat_id = ?",
            (role_name, chat_id),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            await self._connection.execute(
                "DELETE FROM role_assignments WHERE role_name = ? AND chat_id = ?",
                (role_name, chat_id),
            )
            await self._connection.commit()
            return False

        await self._connection.execute(
            "INSERT INTO role_assignments (role_name, chat_id) VALUES (?, ?)",
            (role_name, chat_id),
        )
        await self._connection.commit()
        return True

    async def clear_role(self, role_name: str) -> None:
        await self._connection.execute(
            "DELETE FROM role_assignments WHERE role_name = ?", (role_name,)
        )
        await self._connection.commit()

    async def try_claim_order(self, order_id: str, chat_id: int) -> int:
        """Атомарно фиксирует, кто первый нажал «Взять».

        PRIMARY KEY на order_id гарантирует, что выиграет только одна
        вставка, даже если два клика пришли почти одновременно.
        """
        await self._connection.execute(
            "INSERT OR IGNORE INTO claim_locks (order_id, chat_id, claimed_at) "
            "VALUES (?, ?, ?)",
            (order_id, chat_id, datetime.now(timezone.utc).isoformat()),
        )
        await self._connection.commit()
        cursor = await self._connection.execute(
            "SELECT chat_id FROM claim_locks WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        return row[0]

    async def get_claim(self, order_id: str) -> int | None:
        cursor = await self._connection.execute(
            "SELECT chat_id FROM claim_locks WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def clear_claim(self, order_id: str) -> None:
        await self._connection.execute(
            "DELETE FROM claim_locks WHERE order_id = ?", (order_id,)
        )
        await self._connection.commit()

    async def list_claims_for_user(self, chat_id: int) -> list[str]:
        cursor = await self._connection.execute(
            "SELECT order_id FROM claim_locks WHERE chat_id = ?", (chat_id,)
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def get_user_display_name(self, chat_id: int) -> str:
        cursor = await self._connection.execute(
            "SELECT username, display_name FROM users WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return str(chat_id)
        username, display_name = row
        return display_name or (f"@{username}" if username else str(chat_id))

    async def save_claim_broadcast(
        self, order_id: str, chat_id: int, message_id: int
    ) -> None:
        await self._connection.execute(
            "INSERT OR REPLACE INTO claim_broadcasts (order_id, chat_id, message_id) "
            "VALUES (?, ?, ?)",
            (order_id, chat_id, message_id),
        )
        await self._connection.commit()

    async def get_claim_broadcasts(self, order_id: str) -> list[tuple[int, int]]:
        cursor = await self._connection.execute(
            "SELECT chat_id, message_id FROM claim_broadcasts WHERE order_id = ?",
            (order_id,),
        )
        return await cursor.fetchall()

    async def clear_claim_broadcasts(self, order_id: str) -> None:
        await self._connection.execute(
            "DELETE FROM claim_broadcasts WHERE order_id = ?", (order_id,)
        )
        await self._connection.commit()

    async def save_ms_account(self, chat_id: int, ms_user_id: str, ms_email: str | None) -> None:
        await self._connection.execute(
            "INSERT OR REPLACE INTO ms_accounts (chat_id, ms_user_id, ms_email, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, ms_user_id, ms_email, datetime.now(timezone.utc).isoformat()),
        )
        await self._connection.commit()

    async def get_ms_account(self, chat_id: int) -> dict | None:
        cursor = await self._connection.execute(
            "SELECT ms_user_id, ms_email FROM ms_accounts WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {"ms_user_id": row[0], "ms_email": row[1]}

    async def get_created_task(self, order_id: str, role_name: str) -> str | None:
        cursor = await self._connection.execute(
            "SELECT task_id FROM tasks_created WHERE order_id = ? AND role_name = ?",
            (order_id, role_name),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def save_created_task(
        self, order_id: str, role_name: str, list_id: str, task_id: str
    ) -> None:
        await self._connection.execute(
            "INSERT OR REPLACE INTO tasks_created "
            "(order_id, role_name, list_id, task_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (order_id, role_name, list_id, task_id, datetime.now(timezone.utc).isoformat()),
        )
        await self._connection.commit()

    async def delete_cached_order(self, order_id: str) -> None:
        await self._connection.execute("DELETE FROM order_cache WHERE order_id = ?", (order_id,))
        await self._connection.commit()

    async def delete_order_data(self, order_id: str, shoot_ids: list[str]) -> list[tuple[str, str]]:
        """Стирает всё, что бот знает о заказе и его съёмках.

        Возвращает (list_id, task_id) задач To Do, которые нужно удалить.
        """
        conn = self._connection
        cursor = await conn.execute(
            "SELECT list_id, task_id FROM tasks_created WHERE order_id = ?", (order_id,)
        )
        tasks = [tuple(row) for row in await cursor.fetchall()]
        for shoot_id in shoot_ids:
            cursor = await conn.execute(
                "SELECT list_id, task_id FROM shoot_tasks WHERE shoot_id = ?", (shoot_id,)
            )
            tasks += [tuple(row) for row in await cursor.fetchall()]
            for table in ("shoot_cache", "shoot_claims", "shoot_tasks"):
                await conn.execute(f"DELETE FROM {table} WHERE shoot_id = ?", (shoot_id,))
            await conn.execute("DELETE FROM claim_broadcasts WHERE order_id = ?", (shoot_id,))
        for table in (
            "order_cache", "status_history", "tasks_created", "tasks_completed",
            "reminder_log", "claim_locks", "claim_broadcasts", "shoot_cache",
        ):
            await conn.execute(f"DELETE FROM {table} WHERE order_id = ?", (order_id,))
        await conn.commit()
        return tasks

    # --- Съёмки -----------------------------------------------------------

    async def get_cached_shoot(self, shoot_id: str) -> Shoot | None:
        cursor = await self._connection.execute(
            "SELECT snapshot FROM shoot_cache WHERE shoot_id = ?", (shoot_id,)
        )
        row = await cursor.fetchone()
        return Shoot(**json.loads(row[0])) if row else None

    async def save_shoot(self, shoot: Shoot) -> None:
        await self._connection.execute(
            "INSERT OR REPLACE INTO shoot_cache (shoot_id, order_id, snapshot) VALUES (?, ?, ?)",
            (shoot.shoot_id, shoot.order_id, json.dumps(asdict(shoot), ensure_ascii=False)),
        )
        await self._connection.commit()

    async def delete_cached_shoot(self, shoot_id: str) -> None:
        await self._connection.execute("DELETE FROM shoot_cache WHERE shoot_id = ?", (shoot_id,))
        await self._connection.commit()

    async def list_cached_shoots(self) -> list[Shoot]:
        cursor = await self._connection.execute("SELECT snapshot FROM shoot_cache")
        return [Shoot(**json.loads(row[0])) for row in await cursor.fetchall()]

    async def list_shoots_for_order(self, order_id: str) -> list[Shoot]:
        cursor = await self._connection.execute(
            "SELECT snapshot FROM shoot_cache WHERE order_id = ?", (order_id,)
        )
        return [Shoot(**json.loads(row[0])) for row in await cursor.fetchall()]

    async def try_claim_shoot(self, shoot_id: str, chat_id: int) -> int:
        """Атомарно: кто первый нажал «Взять» на эту съёмку (PRIMARY KEY)."""
        await self._connection.execute(
            "INSERT OR IGNORE INTO shoot_claims (shoot_id, chat_id, claimed_at) VALUES (?, ?, ?)",
            (shoot_id, chat_id, datetime.now(timezone.utc).isoformat()),
        )
        await self._connection.commit()
        cursor = await self._connection.execute(
            "SELECT chat_id FROM shoot_claims WHERE shoot_id = ?", (shoot_id,)
        )
        return (await cursor.fetchone())[0]

    async def set_shoot_claim(self, shoot_id: str, chat_id: int) -> None:
        """Назначение владельцем — перебивает прежний claim."""
        await self._connection.execute(
            "INSERT OR REPLACE INTO shoot_claims (shoot_id, chat_id, claimed_at) VALUES (?, ?, ?)",
            (shoot_id, chat_id, datetime.now(timezone.utc).isoformat()),
        )
        await self._connection.commit()

    async def get_shoot_claim(self, shoot_id: str) -> int | None:
        cursor = await self._connection.execute(
            "SELECT chat_id FROM shoot_claims WHERE shoot_id = ?", (shoot_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def clear_shoot_claim(self, shoot_id: str) -> None:
        await self._connection.execute("DELETE FROM shoot_claims WHERE shoot_id = ?", (shoot_id,))
        await self._connection.commit()

    async def get_shoot_task(self, shoot_id: str) -> tuple[str, str, str] | None:
        """(list_id, task_id, signature) задачи To Do по съёмке."""
        cursor = await self._connection.execute(
            "SELECT list_id, task_id, signature FROM shoot_tasks WHERE shoot_id = ?", (shoot_id,)
        )
        row = await cursor.fetchone()
        return tuple(row) if row else None

    async def save_shoot_task(self, shoot_id: str, list_id: str, task_id: str, signature: str) -> None:
        await self._connection.execute(
            "INSERT OR REPLACE INTO shoot_tasks (shoot_id, list_id, task_id, signature) "
            "VALUES (?, ?, ?, ?)",
            (shoot_id, list_id, task_id, signature),
        )
        await self._connection.commit()

    async def delete_shoot_task(self, shoot_id: str) -> None:
        await self._connection.execute("DELETE FROM shoot_tasks WHERE shoot_id = ?", (shoot_id,))
        await self._connection.commit()

    async def list_shoot_task_ids(self) -> list[str]:
        cursor = await self._connection.execute("SELECT shoot_id FROM shoot_tasks")
        return [row[0] for row in await cursor.fetchall()]

    # --- Настройки и напоминания --------------------------------------------

    async def get_reminder_hours(self, chat_id: int) -> int | None:
        """None — человек ещё не выбирал, значит интервал по умолчанию."""
        cursor = await self._connection.execute(
            "SELECT reminder_hours FROM user_settings WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_reminder_hours(self, chat_id: int, hours: int) -> None:
        await self._connection.execute(
            "INSERT OR REPLACE INTO user_settings (chat_id, reminder_hours) VALUES (?, ?)",
            (chat_id, hours),
        )
        await self._connection.commit()

    async def get_last_reminder(self, chat_id: int, order_id: str, status: str) -> str | None:
        cursor = await self._connection.execute(
            "SELECT sent_at FROM reminder_log WHERE chat_id = ? AND order_id = ? AND status = ?",
            (chat_id, order_id, status),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_last_reminder(self, chat_id: int, order_id: str, status: str, sent_at: str) -> None:
        await self._connection.execute(
            "INSERT OR REPLACE INTO reminder_log (chat_id, order_id, status, sent_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, order_id, status, sent_at),
        )
        await self._connection.commit()

    async def was_notice_sent(self, key: str) -> bool:
        cursor = await self._connection.execute(
            "SELECT 1 FROM sent_notices WHERE notice_key = ?", (key,)
        )
        return await cursor.fetchone() is not None

    async def mark_notice_sent(self, key: str) -> None:
        await self._connection.execute(
            "INSERT OR IGNORE INTO sent_notices (notice_key, sent_at) VALUES (?, ?)",
            (key, datetime.now(timezone.utc).isoformat()),
        )
        await self._connection.commit()

    async def list_open_tasks(self) -> list[tuple[str, str, str, str]]:
        """(order_id, role_name, list_id, task_id) задач, которые бот ещё не закрыл."""
        cursor = await self._connection.execute(
            "SELECT t.order_id, t.role_name, t.list_id, t.task_id FROM tasks_created t "
            "LEFT JOIN tasks_completed c "
            "ON c.order_id = t.order_id AND c.role_name = t.role_name "
            "WHERE c.order_id IS NULL"
        )
        return [tuple(row) for row in await cursor.fetchall()]

    async def mark_task_completed(self, order_id: str, role_name: str) -> None:
        await self._connection.execute(
            "INSERT OR REPLACE INTO tasks_completed (order_id, role_name, completed_at) "
            "VALUES (?, ?, ?)",
            (order_id, role_name, datetime.now(timezone.utc).isoformat()),
        )
        await self._connection.commit()

    async def list_cached_orders(self) -> list[Order]:
        cursor = await self._connection.execute("SELECT snapshot FROM order_cache")
        rows = await cursor.fetchall()
        return [Order(**json.loads(row[0])) for row in rows]

    async def get_status_since(self, order_id: str) -> tuple[str, str] | None:
        cursor = await self._connection.execute(
            "SELECT status, since FROM status_history WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        return (row[0], row[1]) if row else None

    async def set_status_since(self, order_id: str, status: str, since: str) -> None:
        await self._connection.execute(
            "INSERT OR REPLACE INTO status_history (order_id, status, since) VALUES (?, ?, ?)",
            (order_id, status, since),
        )
        await self._connection.commit()

    async def get_last_daily_check_date(self) -> str | None:
        cursor = await self._connection.execute(
            "SELECT last_run_date FROM daily_check_state WHERE id = 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_last_daily_check_date(self, date_iso: str) -> None:
        await self._connection.execute(
            "INSERT INTO daily_check_state (id, last_run_date) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_run_date = excluded.last_run_date",
            (date_iso,),
        )
        await self._connection.commit()
