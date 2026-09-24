from __future__ import annotations

import asyncio
import secrets
import sqlite3
import string
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from discord.ext import commands

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "checks.db"
KEYS_DB_PATH = DATA_DIR / "keys.db"

_initialized = False
_blacklisted_users: set[int] = set()
_disabled_commands: set[str] = set()
_premium_users: dict[int, int] = {}
_cache_lock = RLock()
_database_lock = RLock()
_cache_refresh_lock = RLock()


@dataclass(slots=True)
class PremiumKey:
    key: str
    premium_duration: int
    key_expires_at: int
    created_at: int
    used_by: int | None
    used_at: int | None
    revoked_at: int | None


class CommandDisabled(commands.CommandError):
    """Raised when a command is disabled."""


class UserBlacklisted(commands.CommandError):
    """Raised when a user is blacklisted."""


class PremiumRequired(commands.CommandError):
    """Raised when a user needs premium to use a command."""


def _now() -> int:
    return int(time.time())


def _normalize_command_name(name: str) -> str:
    return " ".join(name.lower().strip().split())


def parse_duration(value: str) -> int:
    value = value.strip().lower()
    if len(value) < 2:
        raise ValueError("Duration must look like 1d, 1w, 1m, or 1y.")

    amount_text = value[:-1]
    unit = value[-1]
    if not amount_text.isdigit():
        raise ValueError("Duration amount must be a number.")

    amount = int(amount_text)
    if amount <= 0:
        raise ValueError("Duration must be greater than zero.")

    multipliers = {
        "h": 60 * 60,
        "d": 24 * 60 * 60,
        "w": 7 * 24 * 60 * 60,
        "m": 30 * 24 * 60 * 60,
        "y": 365 * 24 * 60 * 60,
    }
    if unit not in multipliers:
        raise ValueError("Supported duration units: h, d, w, m, y.")
    return amount * multipliers[unit]


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.row_factory = sqlite3.Row
    return conn


def _connect_keys() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(KEYS_DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _database_lock:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blacklisted_users (
                    user_id INTEGER PRIMARY KEY,
                    reason TEXT,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS disabled_commands (
                    command_name TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS premium_users (
                    user_id INTEGER PRIMARY KEY,
                    expires_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(premium_users)").fetchall()}
            if "expires_at" not in columns:
                conn.execute("ALTER TABLE premium_users ADD COLUMN expires_at INTEGER")
                conn.execute(
                    """
                    UPDATE premium_users
                    SET expires_at = updated_at + (COALESCE(balance, 0) * 30 * 24 * 60 * 60)
                    WHERE expires_at IS NULL
                    """
                )
            conn.execute("DELETE FROM premium_users WHERE expires_at IS NULL OR expires_at <= ?", (_now(),))

        with _connect_keys() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS premium_keys (
                    key TEXT PRIMARY KEY,
                    premium_duration INTEGER NOT NULL,
                    key_expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    used_by INTEGER,
                    used_at INTEGER,
                    revoked_at INTEGER
                )
                """
            )


def _refresh_cache_locked() -> None:
    global _blacklisted_users, _disabled_commands, _initialized, _premium_users

    with _database_lock:
        init_db()
        with _connect() as conn:
            blacklisted_users = {
                int(row["user_id"]) for row in conn.execute("SELECT user_id FROM blacklisted_users").fetchall()
            }
            disabled_commands = {
                str(row["command_name"])
                for row in conn.execute("SELECT command_name FROM disabled_commands").fetchall()
            }
            premium_users = {
                int(row["user_id"]): int(row["expires_at"])
                for row in conn.execute(
                    "SELECT user_id, expires_at FROM premium_users WHERE expires_at > ?", (_now(),)
                ).fetchall()
            }

        with _cache_lock:
            _blacklisted_users = blacklisted_users
            _disabled_commands = disabled_commands
            _premium_users = premium_users
            _initialized = True


def refresh_cache() -> None:
    with _cache_refresh_lock:
        _refresh_cache_locked()


async def initialize_checks() -> None:
    await asyncio.to_thread(refresh_cache)


def _ensure_cache() -> None:
    with _cache_lock:
        initialized = _initialized
    if initialized:
        return
    with _cache_refresh_lock:
        with _cache_lock:
            if _initialized:
                return
        _refresh_cache_locked()


def is_user_blacklisted(user_id: int) -> bool:
    _ensure_cache()
    with _cache_lock:
        return int(user_id) in _blacklisted_users


def is_command_disabled(command_name: str) -> bool:
    _ensure_cache()
    with _cache_lock:
        return _normalize_command_name(command_name) in _disabled_commands


def get_premium_expires_at(user_id: int) -> int | None:
    _ensure_cache()
    with _cache_lock:
        user_id = int(user_id)
        expires_at = _premium_users.get(user_id)
        if expires_at is None:
            return None
        if expires_at <= _now():
            _premium_users.pop(user_id, None)
            return None
        return expires_at


def has_premium(user_id: int) -> bool:
    return get_premium_expires_at(user_id) is not None


def blacklist_user(user_id: int, reason: str | None = None) -> None:
    user_id = int(user_id)
    with _database_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO blacklisted_users (user_id, reason, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason
                """,
                (user_id, reason, _now()),
            )
        with _cache_lock:
            _blacklisted_users.add(user_id)


def unblacklist_user(user_id: int) -> bool:
    user_id = int(user_id)
    with _database_lock:
        with _connect() as conn:
            cursor = conn.execute("DELETE FROM blacklisted_users WHERE user_id = ?", (user_id,))
        with _cache_lock:
            _blacklisted_users.discard(user_id)
    return cursor.rowcount > 0


def disable_command(command_name: str) -> str:
    command_name = _normalize_command_name(command_name)
    with _database_lock:
        with _connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO disabled_commands (command_name, created_at)
                VALUES (?, ?)
                """,
                (command_name, _now()),
            )
        with _cache_lock:
            _disabled_commands.add(command_name)
    return command_name


def enable_command(command_name: str) -> bool:
    command_name = _normalize_command_name(command_name)
    with _database_lock:
        with _connect() as conn:
            cursor = conn.execute("DELETE FROM disabled_commands WHERE command_name = ?", (command_name,))
        with _cache_lock:
            _disabled_commands.discard(command_name)
    return cursor.rowcount > 0


def add_premium(user_id: int, duration: str | int) -> int:
    duration_seconds = parse_duration(duration) if isinstance(duration, str) else max(int(duration), 1)
    user_id = int(user_id)
    _ensure_cache()
    with _database_lock:
        now = _now()
        with _cache_lock:
            current_expires_at = _premium_users.get(user_id) or now
        expires_at = max(current_expires_at, now) + duration_seconds
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO premium_users (user_id, expires_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (user_id, expires_at, now),
            )
        with _cache_lock:
            _premium_users[user_id] = expires_at
        return expires_at


def remove_premium(user_id: int, duration: str | int) -> int | None:
    duration_seconds = parse_duration(duration) if isinstance(duration, str) else max(int(duration), 1)
    user_id = int(user_id)
    _ensure_cache()
    with _database_lock:
        with _cache_lock:
            current_expires_at = _premium_users.get(user_id)
        if current_expires_at is None:
            return None

        expires_at = current_expires_at - duration_seconds
        now = _now()
        if expires_at <= now:
            with _connect() as conn:
                conn.execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))
            with _cache_lock:
                _premium_users.pop(user_id, None)
            return None

        with _connect() as conn:
            conn.execute(
                "UPDATE premium_users SET expires_at = ?, updated_at = ? WHERE user_id = ?",
                (expires_at, now, user_id),
            )
        with _cache_lock:
            _premium_users[user_id] = expires_at
        return expires_at


def revoke_premium(user_id: int) -> bool:
    user_id = int(user_id)
    with _database_lock:
        with _connect() as conn:
            cursor = conn.execute("DELETE FROM premium_users WHERE user_id = ?", (user_id,))
        with _cache_lock:
            _premium_users.pop(user_id, None)
    return cursor.rowcount > 0


def cleanup_expired_premium() -> int:
    now = _now()
    with _database_lock:
        with _connect() as conn:
            rows = conn.execute("SELECT user_id FROM premium_users WHERE expires_at <= ?", (now,)).fetchall()
            conn.execute("DELETE FROM premium_users WHERE expires_at <= ?", (now,))
        with _cache_lock:
            for row in rows:
                _premium_users.pop(int(row["user_id"]), None)
    return len(rows)


def generate_premium_keys(premium_duration: str, key_lifespan: str, count: int = 1) -> list[PremiumKey]:
    premium_duration_seconds = parse_duration(premium_duration)
    key_lifespan_seconds = parse_duration(key_lifespan)
    count = min(max(int(count), 1), 25)
    alphabet = string.ascii_uppercase + string.digits
    keys: list[PremiumKey] = []
    now = _now()
    key_expires_at = now + key_lifespan_seconds

    with _connect_keys() as conn:
        while len(keys) < count:
            raw_key = "".join(secrets.choice(alphabet) for _ in range(24))
            key = f"AMN-{raw_key[:6]}-{raw_key[6:12]}-{raw_key[12:18]}-{raw_key[18:]}"
            try:
                conn.execute(
                    """
                    INSERT INTO premium_keys (key, premium_duration, key_expires_at, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, premium_duration_seconds, key_expires_at, now),
                )
            except sqlite3.IntegrityError:
                continue
            keys.append(
                PremiumKey(
                    key=key,
                    premium_duration=premium_duration_seconds,
                    key_expires_at=key_expires_at,
                    created_at=now,
                    used_by=None,
                    used_at=None,
                    revoked_at=None,
                )
            )
    return keys


def revoke_premium_key(key: str) -> bool:
    key = key.strip().upper()
    with _connect_keys() as conn:
        cursor = conn.execute(
            "UPDATE premium_keys SET revoked_at = ? WHERE key = ? AND revoked_at IS NULL",
            (_now(), key),
        )
    return cursor.rowcount > 0


def _premium_key_from_row(row: sqlite3.Row) -> PremiumKey:
    return PremiumKey(
        key=str(row["key"]),
        premium_duration=int(row["premium_duration"]),
        key_expires_at=int(row["key_expires_at"]),
        created_at=int(row["created_at"]),
        used_by=int(row["used_by"]) if row["used_by"] is not None else None,
        used_at=int(row["used_at"]) if row["used_at"] is not None else None,
        revoked_at=int(row["revoked_at"]) if row["revoked_at"] is not None else None,
    )


def list_premium_keys(limit: int = 10) -> list[PremiumKey]:
    limit = min(max(int(limit), 1), 50)
    with _connect_keys() as conn:
        rows = conn.execute(
            """
            SELECT key, premium_duration, key_expires_at, created_at, used_by, used_at, revoked_at
            FROM premium_keys
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_premium_key_from_row(row) for row in rows]


def redeem_premium_key(user_id: int, key: str) -> int:
    user_id = int(user_id)
    key = key.strip().upper()
    now = _now()
    with _connect_keys() as conn:
        row = conn.execute(
            """
            SELECT key, premium_duration, key_expires_at, created_at, used_by, used_at, revoked_at
            FROM premium_keys
            WHERE key = ?
            """,
            (key,),
        ).fetchone()
        if row is None:
            raise ValueError("Premium key was not found.")
        premium_key = _premium_key_from_row(row)
        if premium_key.revoked_at is not None:
            raise ValueError("Premium key has been revoked.")
        if premium_key.used_at is not None:
            raise ValueError("Premium key has already been used.")
        if premium_key.key_expires_at <= now:
            raise ValueError("Premium key has expired.")

        expires_at = add_premium(user_id, premium_key.premium_duration)
        conn.execute(
            "UPDATE premium_keys SET used_by = ?, used_at = ? WHERE key = ?",
            (user_id, now, key),
        )
    return expires_at


def _command_lookup_names(command: commands.Command | None) -> list[str]:
    if command is None:
        return []
    parts = _normalize_command_name(command.qualified_name).split()
    return [" ".join(parts[:index]) for index in range(len(parts), 0, -1)]


async def command_enabled_predicate(ctx: commands.Context) -> bool:
    if await ctx.bot.is_owner(ctx.author):
        return True
    for name in _command_lookup_names(ctx.command):
        if is_command_disabled(name):
            raise CommandDisabled(f"The `{name}` command is disabled by developer.")
    return True


def command_enabled() -> commands.Check:
    return commands.check(command_enabled_predicate)


async def user_not_blacklisted_predicate(ctx: commands.Context) -> bool:
    if await ctx.bot.is_owner(ctx.author):
        return True
    if is_user_blacklisted(ctx.author.id):
        raise UserBlacklisted("You are blacklisted from using this bot. Contact support.")
    return True


def user_not_blacklisted() -> commands.Check:
    return commands.check(user_not_blacklisted_predicate)


def premium_required() -> commands.Check:
    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        if not has_premium(ctx.author.id):
            raise PremiumRequired("This command requires premium. buy premium at https://amenity.qzz.io/premium")
        return True

    return commands.check(predicate)


premium_only = premium_required
