from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from collections.abc import Mapping

JsonData = dict[str, Any] | list[Any]

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)


def create_http_session(*, timeout: aiohttp.ClientTimeout | None = None) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=timeout or DEFAULT_TIMEOUT)


async def close_http_session(session: aiohttp.ClientSession) -> None:
    if session.closed:
        return

    await session.close()


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: aiohttp.ClientTimeout | None = DEFAULT_TIMEOUT,
    expected_status: int = 200,
) -> tuple[JsonData | None, int | None]:
    try:
        async with session.get(url, headers=headers, timeout=timeout) as response:
            status = response.status
            if status != expected_status:
                return None, status
            return await response.json(), status
    except (TimeoutError, aiohttp.ClientError):
        return None, None
    except asyncio.CancelledError:
        raise
    except Exception:
        # log_exception(exc)
        return None, None
