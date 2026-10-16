"""Regression tests for audit fixes (verified red before each fix, green after)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import core.checks as checks
from api.http import fetch_json
from core.cache import TimeCache

if TYPE_CHECKING:
    from pathlib import Path


def test_redeem_premium_key_grants_exactly_once_under_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checks, "DB_PATH", tmp_path / "checks.db")
    monkeypatch.setattr(checks, "KEYS_DB_PATH", tmp_path / "keys.db")
    monkeypatch.setattr(checks, "_initialized", False)
    checks._blacklisted_users.clear()
    checks._disabled_commands.clear()
    checks._premium_users.clear()
    checks.init_db()
    key = checks.generate_premium_keys("30d", "1d", 1)[0].key

    barrier = threading.Barrier(16)
    granted: list[int] = []
    denied = 0
    denied_lock = threading.Lock()

    def attempt(index: int) -> None:
        nonlocal denied
        barrier.wait(timeout=10)  # maximize overlap of the claim window
        try:
            checks.redeem_premium_key(5000 + index, key)
        except ValueError:
            with denied_lock:
                denied += 1
        else:
            granted.append(index)

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(attempt, range(16)))

    assert len(granted) == 1
    assert denied == 15
    assert checks.get_premium_expires_at(5000 + granted[0]) is not None


def test_time_cache_prune_returns_a_copy() -> None:
    cache: TimeCache[str] = TimeCache(default_ttl=60)
    cache.set("key", "value")

    exposed = cache.prune()
    exposed["injected"] = ("value", 9999999999.0)

    assert cache.get("injected") is None
    assert len(cache) == 1


class _FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    async def json(self) -> object:
        return self.payload


class _FakeRequest:
    def __init__(self, response: _FakeResponse | BaseException) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeSession:
    def __init__(self, result: _FakeResponse | BaseException) -> None:
        self.result = result

    def get(self, url: str, **kwargs: object) -> _FakeRequest:
        return _FakeRequest(self.result)


@pytest.mark.asyncio
async def test_fetch_json_logs_unexpected_errors(caplog: pytest.LogCaptureFixture) -> None:
    session = _FakeSession(ValueError("weird decode bug"))

    with caplog.at_level(logging.ERROR, logger="api.http"):
        data, status = await fetch_json(session, "https://service.example.test/data")  # type: ignore[arg-type]

    assert (data, status) == (None, None)
    assert "weird decode bug" in caplog.text


@pytest.mark.asyncio
async def test_hybrid_commands_log_exactly_once_per_source(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.amenity as amenity_module

    calls: list[str] = []

    async def fake_usage(ctx: object) -> None:
        calls.append(f"prefix:{ctx.author.id}")  # type: ignore[union-attr]

    async def fake_app_usage(interaction: object, command: object) -> None:
        calls.append(f"slash:{command.name}")  # type: ignore[union-attr]

    def fake_track(user: object) -> None:
        calls.append(f"track:{user.id}")  # type: ignore[union-attr]

    monkeypatch.setattr(amenity_module, "log_command_usage", fake_usage)
    monkeypatch.setattr(amenity_module, "log_app_command_usage", fake_app_usage)
    monkeypatch.setattr(amenity_module, "track_installed_user", fake_track)

    author = SimpleNamespace(id=7)
    # Pure prefix invocation: one prefix log.
    await amenity_module.Amenity.on_command_completion(
        None, SimpleNamespace(interaction=None, author=author)  # type: ignore[arg-type]
    )
    # Slash-invoked hybrid: prefix handler must stay silent (app handler owns it).
    await amenity_module.Amenity.on_command_completion(
        None, SimpleNamespace(interaction=object(), author=author)  # type: ignore[arg-type]
    )
    # Pure slash + slash hybrid: one slash log each.
    await amenity_module.Amenity.on_app_command_completion(
        None, SimpleNamespace(user=author), SimpleNamespace(name="ping")  # type: ignore[arg-type]
    )
    await amenity_module.Amenity.on_app_command_completion(
        None, SimpleNamespace(user=author), SimpleNamespace(name="qr")  # type: ignore[arg-type]
    )

    assert calls == ["track:7", "prefix:7", "track:7", "slash:ping", "track:7", "slash:qr"]
