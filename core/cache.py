import asyncio
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import RLock
from typing import TypeVar

T = TypeVar("T")
_MISSING = object()
_SINGLE_FLIGHT_WAIT_TIMEOUT = 30.0
_CACHE_PRUNE_INTERVAL_SECONDS = 1.0


@dataclass
class _Flight:
    done: threading.Event
    error: BaseException | None = None


@dataclass
class _AsyncLockEntry:
    lock: asyncio.Lock
    users: int = 0


class TimeCache[T]:
    def __init__(self, default_ttl: float = 60.0) -> None:
        self.default_ttl = float(default_ttl)
        self._store: dict[str, tuple[T, float]] = {}
        self._lock = RLock()
        self._inflight: dict[str, _Flight] = {}
        self._async_locks: dict[str, _AsyncLockEntry] = {}
        self._next_prune_at = 0.0

    def __len__(self) -> int:
        with self._lock:
            self.prune()
            return len(self._store)

    def _expires_at(self, ttl: float | None) -> float:
        if ttl is None:
            ttl = self.default_ttl
        return time.monotonic() + float(ttl)

    def prune(self) -> dict[str, tuple[T, float]]:
        with self._lock:
            now = time.monotonic()
            self._prune_expired_locked(now)
            self._next_prune_at = now + _CACHE_PRUNE_INTERVAL_SECONDS
            return self._store

    def _prune_expired_locked(self, now: float) -> None:
        expired_keys = [key for key, (_, expires_at) in self._store.items() if expires_at <= now]
        for key in expired_keys:
            self._store.pop(key, None)

    def set(self, key: str, value: T, ttl: float | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            if now >= self._next_prune_at:
                self._prune_expired_locked(now)
                self._next_prune_at = now + _CACHE_PRUNE_INTERVAL_SECONDS
            expires = self._expires_at(ttl)
            self._store[key] = (value, expires)

    def get(self, key: str, default: T | object = None) -> T | object:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return default
            value, expires_at = item
            if expires_at <= time.monotonic():
                self._store.pop(key, None)
                return default
            return value

    def has(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store = {}
            self._next_prune_at = 0.0

    def get_or_set(self, key: str, factory: Callable[[], T], ttl: float | None = None) -> T:
        while True:
            with self._lock:
                value = self._store.get(key)
                if value is not None and value[1] > time.monotonic():
                    return value[0]
                flight = self._inflight.get(key)
                owner = flight is None
                if owner:
                    flight = _Flight(threading.Event())
                    self._inflight[key] = flight

            if not owner:
                assert flight is not None
                if not flight.done.wait(timeout=_SINGLE_FLIGHT_WAIT_TIMEOUT):
                    raise TimeoutError(f"Timed out waiting for cache factory for {key!r}")
                if flight.error is not None:
                    continue
                continue

            try:
                value = factory()
                if inspect.isawaitable(value):
                    raise TypeError("factory returned awaitable; use get_or_set_async")
                self.set(key, value, ttl=ttl)
                return value
            except BaseException as exc:
                with self._lock:
                    flight.error = exc
                raise
            finally:
                with self._lock:
                    if self._inflight.get(key) is flight:
                        self._inflight.pop(key, None)
                    flight.done.set()

    def _get_async_lock_entry(self, key: str) -> _AsyncLockEntry:
        with self._lock:
            entry = self._async_locks.get(key)
            if entry is None:
                entry = _AsyncLockEntry(asyncio.Lock())
                self._async_locks[key] = entry
            entry.users += 1
            return entry

    async def get_or_set_async(
        self,
        key: str,
        factory: Callable[[], T | Awaitable[T]],
        ttl: float | None = None,
        *,
        cache_if: Callable[[T], bool] | None = None,
    ) -> T:
        entry = self._get_async_lock_entry(key)
        try:
            async with entry.lock:
                value = self.get(key, _MISSING)
                if value is not _MISSING:
                    return value  # type: ignore[return-value]
                value = factory()
                if inspect.isawaitable(value):
                    value = await value
                if cache_if is None or cache_if(value):
                    self.set(key, value, ttl=ttl)
                return value
        finally:
            with self._lock:
                entry.users -= 1
                if entry.users == 0 and self._async_locks.get(key) is entry:
                    self._async_locks.pop(key, None)


cache: TimeCache[object] = TimeCache()
