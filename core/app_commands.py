"""Persistence and formatting helpers for registered application command IDs.

Discord only renders a clickable slash-command mention (``</name:id>``) when the
command's ID is known. The IDs fetched at startup are therefore kept in memory,
mirrored into the shared cache, and persisted to disk so mentions still work on
the next boot before the first fetch completes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from core.cache import cache

if TYPE_CHECKING:
    from collections.abc import Mapping

APP_COMMAND_IDS_PATH = Path(__file__).resolve().parents[1] / "artifacts" / "app_command_ids.json"
APP_COMMAND_IDS_CACHE_KEY = "discord:app_command_ids"
APP_COMMAND_IDS_CACHE_TTL = 86400


def load_persisted_command_ids() -> dict[str, int]:
    """Read the command IDs saved by a previous run, ignoring a missing or corrupt file."""
    try:
        if APP_COMMAND_IDS_PATH.exists():
            data = json.loads(APP_COMMAND_IDS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(name): int(value) for name, value in data.items() if str(value).isdigit()}
    except (OSError, ValueError):
        pass
    return {}


def save_persisted_command_ids(command_ids: Mapping[str, int]) -> None:
    """Best-effort persist of command IDs so mentions survive a restart."""
    try:
        APP_COMMAND_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        APP_COMMAND_IDS_PATH.write_text(json.dumps(dict(command_ids), indent=2), encoding="utf-8")
    except OSError:
        pass


def resolve_command_id(command_ids: Mapping[str, int], root_name: str) -> int | None:
    """Look a command ID up in memory, then the shared cache, then disk."""
    if root_name in command_ids:
        return command_ids[root_name]

    cached = cache.get(APP_COMMAND_IDS_CACHE_KEY)
    if isinstance(cached, dict) and root_name in cached:
        return cached[root_name]

    persisted = load_persisted_command_ids()
    if persisted:
        # Warm the cache so a page of mentions reads the file at most once.
        cache.set(APP_COMMAND_IDS_CACHE_KEY, persisted, ttl=APP_COMMAND_IDS_CACHE_TTL)
    return persisted.get(root_name)


def command_mention(command_name: str, command_ids: Mapping[str, int], *, prefix: str = "/") -> str:
    """Render a clickable ``</name:id>`` mention, or a plain code span without an ID."""
    parts = command_name.strip().split()
    command_id = resolve_command_id(command_ids, parts[0]) if parts else None
    if command_id:
        return f"</{command_name}:{command_id}>"
    return f"`{prefix}{command_name}`"
