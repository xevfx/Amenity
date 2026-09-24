from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord


async def fetch_user_cached(bot: discord.Client, user_id: int) -> discord.User:
    cached_fetch = getattr(bot, "fetch_user_cached", None)
    if cached_fetch is not None:
        return await cached_fetch(user_id)
    return await bot.fetch_user(user_id)
