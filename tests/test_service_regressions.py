from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import cogs.crypto as crypto_module
import cogs.github as github_module
import cogs.tools as tools_module
from api.http import fetch_json
from core.amenity import Amenity
from core.cache import TimeCache


class FakeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    async def json(self) -> object:
        return self.payload


class FakeRequest:
    def __init__(self, response: FakeResponse | BaseException) -> None:
        self.response = response

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeSession:
    def __init__(self, result: FakeResponse | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeRequest:
        self.calls.append((url, kwargs))
        return FakeRequest(self.result)


@pytest.mark.asyncio
async def test_fetch_json_returns_payload_and_forwards_request_options() -> None:
    session = FakeSession(FakeResponse(200, {"ok": True}))

    data, status = await fetch_json(
        session,  # type: ignore[arg-type]
        "https://service.example.test/data",
        headers={"Authorization": "test-value"},
        expected_status=200,
    )

    assert data == {"ok": True}
    assert status == 200
    assert len(session.calls) == 1
    url, options = session.calls[0]
    assert url == "https://service.example.test/data"
    assert options["headers"] == {"Authorization": "test-value"}
    assert getattr(options["timeout"], "total", None) == 15


@pytest.mark.asyncio
async def test_fetch_json_preserves_http_status_for_unexpected_response() -> None:
    session = FakeSession(FakeResponse(429, {"message": "rate limited"}))

    data, status = await fetch_json(session, "https://service.example.test/data")  # type: ignore[arg-type]

    assert data is None
    assert status == 429


@pytest.mark.asyncio
async def test_fetch_json_logs_unexpected_errors(caplog: pytest.LogCaptureFixture) -> None:
    session = FakeSession(ValueError("weird decode bug"))

    with caplog.at_level(logging.ERROR, logger="api.http"):
        data, status = await fetch_json(session, "https://service.example.test/data")  # type: ignore[arg-type]

    assert (data, status) == (None, None)
    assert "weird decode bug" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), asyncio.CancelledError()])
async def test_fetch_json_handles_timeout_and_propagates_cancellation(error: BaseException) -> None:
    session = FakeSession(error)

    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await fetch_json(session, "https://service.example.test/data")  # type: ignore[arg-type]
    else:
        data, status = await fetch_json(session, "https://service.example.test/data")  # type: ignore[arg-type]
        assert (data, status) == (None, None)


@pytest.mark.asyncio
async def test_async_cache_can_skip_non_cacheable_results() -> None:
    cache_instance: TimeCache[tuple[int, bool]] = TimeCache()
    calls = 0

    async def factory() -> tuple[int, bool]:
        nonlocal calls
        calls += 1
        return calls, calls > 1

    first = await cache_instance.get_or_set_async("result", factory, cache_if=lambda value: value[1])
    second = await cache_instance.get_or_set_async("result", factory, cache_if=lambda value: value[1])
    third = await cache_instance.get_or_set_async("result", factory, cache_if=lambda value: value[1])

    assert (first, second, third) == ((1, False), (2, True), (2, True))
    assert calls == 2


@pytest.mark.asyncio
async def test_amenity_user_fetch_cache_normalizes_ids_and_reuses_result() -> None:
    user = SimpleNamespace(id=123)
    bot = SimpleNamespace(fetch_user=AsyncMock(return_value=user))

    first = await Amenity.fetch_user_cached(bot, 123)  # type: ignore[arg-type]
    second = await Amenity.fetch_user_cached(bot, "123")  # type: ignore[arg-type]

    assert first is second is user
    bot.fetch_user.assert_awaited_once_with(123)


@pytest.mark.asyncio
async def test_github_api_cache_reuses_success_and_retries_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []
    outcomes = iter([(None, 503), ({"login": "octocat"}, 200)])

    async def fake_fetch_json(
        _session: object,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, str] | None, int]:
        del headers
        requests.append(url)
        return next(outcomes)

    monkeypatch.setattr(github_module, "fetch_json", fake_fetch_json)
    cog = SimpleNamespace(aiohttp=object())

    first = await github_module.Github._fetch_json(cog, "https://api.example.test/user", ttl=60)  # type: ignore[arg-type]
    second = await github_module.Github._fetch_json(cog, "https://api.example.test/user", ttl=60)  # type: ignore[arg-type]
    third = await github_module.Github._fetch_json(cog, "https://api.example.test/user", ttl=60)  # type: ignore[arg-type]

    assert first == (None, 503)
    assert second == third == ({"login": "octocat"}, 200)
    assert requests == ["https://api.example.test/user"] * 2


@pytest.mark.asyncio
async def test_crypto_json_cache_only_stores_successful_responses() -> None:
    fetch = AsyncMock(side_effect=[(None, 502), ({"price": 2}, 200)])
    cog = SimpleNamespace(_fetch_json_status=fetch)
    url = "https://api.example.test/price"

    first = await crypto_module.Crypto._fetch_json_status_cached(cog, url)  # type: ignore[arg-type]
    second = await crypto_module.Crypto._fetch_json_status_cached(cog, url)  # type: ignore[arg-type]
    third = await crypto_module.Crypto._fetch_json_status_cached(cog, url)  # type: ignore[arg-type]

    assert first == (None, 502)
    assert second == third == ({"price": 2}, 200)
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_tavily_search_cache_normalizes_query_and_reports_cache_hit() -> None:
    search = AsyncMock(return_value=([{"title": "fixture"}], 200, 1))
    cog = SimpleNamespace(_search_web=search)

    first = await tools_module.Tools._search_web_cached(cog, "A fast bot", "api-key")  # type: ignore[arg-type]
    second = await tools_module.Tools._search_web_cached(cog, "  a FAST   bot ", "api-key")  # type: ignore[arg-type]

    assert first == ([{"title": "fixture"}], 200, 1, False)
    assert second == ([{"title": "fixture"}], 200, 1, True)
    search.assert_awaited_once_with("A fast bot", "api-key")


@pytest.mark.asyncio
async def test_tavily_search_does_not_cache_server_errors() -> None:
    search = AsyncMock(side_effect=[([], 503, 0), ([{"title": "retry"}], 200, 1)])
    cog = SimpleNamespace(_search_web=search)

    first = await tools_module.Tools._search_web_cached(cog, "retry me", "api-key")  # type: ignore[arg-type]
    second = await tools_module.Tools._search_web_cached(cog, "retry me", "api-key")  # type: ignore[arg-type]

    assert first == ([], 503, 0, False)
    assert second == ([{"title": "retry"}], 200, 1, False)
    assert search.await_count == 2
