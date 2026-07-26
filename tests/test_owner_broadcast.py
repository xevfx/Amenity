from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import cogs.owner as owner_module
from cogs.owner import (
    BROADCAST_FOOTER,
    BROADCAST_HEADER,
    MAX_BROADCAST_BODY_LENGTH,
    Owner,
)
from core.installed_users import InstalledUser


class FakeResponse:
    status = 500
    reason = "Test error"
    headers: dict[str, str] = {}


def http_error(error_type: type[discord.HTTPException], status: int) -> discord.HTTPException:
    response = FakeResponse()
    response.status = status
    return error_type(response, {"message": "Test error", "code": 50007})


class FakeDmUser:
    def __init__(self, user_id: int, error: discord.HTTPException | None = None) -> None:
        self.id = user_id
        self.error = error
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def send(self, content: str, **kwargs: object) -> None:
        if self.error is not None:
            raise self.error
        self.messages.append((content, kwargs))


class FakeBot:
    def __init__(
        self,
        *,
        cached: dict[int, FakeDmUser] | None = None,
        fetched: dict[int, FakeDmUser | discord.HTTPException] | None = None,
    ) -> None:
        self.cached = cached or {}
        self.fetched = fetched or {}

    def get_user(self, user_id: int) -> FakeDmUser | None:
        return self.cached.get(user_id)

    async def fetch_user(self, user_id: int) -> FakeDmUser:
        result = self.fetched[user_id]
        if isinstance(result, discord.HTTPException):
            raise result
        return result


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit(self, *, content: str) -> None:
        self.edits.append(content)


def tracked_user(user_id: int) -> InstalledUser:
    return InstalledUser(
        user_id=user_id,
        username=f"user-{user_id}",
        display_name=f"User {user_id}",
        first_seen=1,
        last_seen=2,
        command_count=1,
    )


@pytest.mark.asyncio
async def test_broadcast_accounts_for_delivery_outcomes() -> None:
    sent_user = FakeDmUser(1)
    blocked_user = FakeDmUser(2, http_error(discord.Forbidden, 403))
    failed_user = FakeDmUser(4, http_error(discord.HTTPException, 500))
    bot = FakeBot(
        cached={1: sent_user, 2: blocked_user},
        fetched={
            3: http_error(discord.NotFound, 404),
            4: failed_user,
        },
    )
    cog = Owner(bot)
    progress = FakeMessage()
    payload = f"{BROADCAST_HEADER}Service restored.{BROADCAST_FOOTER}"

    result = await cog._broadcast_tracked_user_dm(
        [tracked_user(user_id) for user_id in range(1, 5)],
        payload,
        progress_message=progress,
        delay=0,
    )

    assert result.total == 4
    assert result.processed == 4
    assert result.sent == 1
    assert result.blocked == 1
    assert result.unavailable == 1
    assert result.failed == 1
    assert sent_user.messages[0][0] == payload
    assert isinstance(sent_user.messages[0][1]["allowed_mentions"], discord.AllowedMentions)
    assert progress.edits


@pytest.mark.asyncio
async def test_dm_users_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = FakeBot(cached={1: FakeDmUser(1)})
    cog = Owner(bot)
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=123),
        reply=AsyncMock(return_value=FakeMessage()),
    )
    broadcast = AsyncMock()
    monkeypatch.setattr(cog, "_broadcast_tracked_user_dm", broadcast)
    monkeypatch.setattr(owner_module, "list_installed_users", lambda: [tracked_user(1)])
    monkeypatch.setattr(owner_module, "confirm_action", AsyncMock(return_value=False))

    await Owner.dm_users.callback(cog, ctx, message="Important issue update")

    broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_users_rejects_oversized_messages() -> None:
    cog = Owner(FakeBot())
    ctx = SimpleNamespace(reply=AsyncMock())

    await Owner.dm_users.callback(cog, ctx, message="x" * (MAX_BROADCAST_BODY_LENGTH + 1))

    ctx.reply.assert_awaited_once()
    assert "too long" in ctx.reply.await_args.args[0]
