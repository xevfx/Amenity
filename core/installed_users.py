from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "installed_users.db"


@dataclass(frozen=True)
class InstalledUser:
    user_id: int
    username: str | None
    display_name: str | None
    first_seen: int
    last_seen: int
    command_count: int


@dataclass
class PendingInstalledUser:
    user_id: int
    username: str | None
    display_name: str | None
    first_seen: int
    last_seen: int
    command_count: int = 0


_pending_users: dict[int, PendingInstalledUser] = {}
_pending_users_lock = Lock()


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.row_factory = sqlite3.Row
    return conn


def init_installed_users_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS installed_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                command_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_installed_users_last_seen ON installed_users(last_seen)")


def track_installed_user(user: discord.abc.User) -> None:
    seen_at = _now()
    user_id = int(user.id)
    username = getattr(user, "name", None)
    display_name = getattr(user, "display_name", None) or getattr(user, "global_name", None)
    with _pending_users_lock:
        pending = _pending_users.get(user_id)
        if pending is None:
            _pending_users[user_id] = PendingInstalledUser(
                user_id=user_id,
                username=username,
                display_name=display_name,
                first_seen=seen_at,
                last_seen=seen_at,
                command_count=1,
            )
            return

        pending.username = username
        pending.display_name = display_name
        pending.last_seen = seen_at
        pending.command_count += 1


def flush_installed_users() -> int:
    init_installed_users_db()
    with _pending_users_lock:
        pending_users = list(_pending_users.values())
        _pending_users.clear()
    if not pending_users:
        return 0

    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO installed_users (user_id, username, display_name, first_seen, last_seen, command_count)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                first_seen = MIN(installed_users.first_seen, excluded.first_seen),
                last_seen = MAX(installed_users.last_seen, excluded.last_seen),
                command_count = installed_users.command_count + excluded.command_count
            """,
            [
                (
                    user.user_id,
                    user.username,
                    user.display_name,
                    user.first_seen,
                    user.last_seen,
                    user.command_count,
                )
                for user in pending_users
            ],
        )
    return len(pending_users)


def list_installed_users(*, include_pending: bool = True) -> list[InstalledUser]:
    init_installed_users_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, username, display_name, first_seen, last_seen, command_count
            FROM installed_users
            ORDER BY last_seen DESC, user_id ASC
            """
        ).fetchall()

    users = {
        int(row["user_id"]): InstalledUser(
            user_id=int(row["user_id"]),
            username=row["username"],
            display_name=row["display_name"],
            first_seen=int(row["first_seen"]),
            last_seen=int(row["last_seen"]),
            command_count=int(row["command_count"]),
        )
        for row in rows
    }
    if include_pending:
        with _pending_users_lock:
            pending_users = list(_pending_users.values())
        for pending in pending_users:
            stored = users.get(pending.user_id)
            if stored is None:
                users[pending.user_id] = InstalledUser(
                    user_id=pending.user_id,
                    username=pending.username,
                    display_name=pending.display_name,
                    first_seen=pending.first_seen,
                    last_seen=pending.last_seen,
                    command_count=pending.command_count,
                )
                continue
            users[pending.user_id] = InstalledUser(
                user_id=stored.user_id,
                username=pending.username or stored.username,
                display_name=pending.display_name or stored.display_name,
                first_seen=min(stored.first_seen, pending.first_seen),
                last_seen=max(stored.last_seen, pending.last_seen),
                command_count=stored.command_count + pending.command_count,
            )
    return sorted(users.values(), key=lambda user: (-user.last_seen, user.user_id))
