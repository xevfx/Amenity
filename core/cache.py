import inspect
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
_MISSING = object()


class TimeCache[T]:
    def __init__(self, default_ttl: float = 60.0) -> None:
        self.default_ttl = float(default_ttl)
        self._store: dict[str, tuple[T, float]] = {}

    def __len__(self) -> int:
        store = self.prune()
        return len(store)

    def _expires_at(self, ttl: float | None) -> float:
        if ttl is None:
            ttl = self.default_ttl
        return time.monotonic() + float(ttl)

    def prune(self) -> dict[str, tuple[T, float]]:
        now = time.monotonic()
        old_store = self._store
        new_store = {k: v for k, v in old_store.items() if v[1] > now}
        if len(new_store) != len(old_store):
            self._store = new_store
            return new_store
        return old_store

    def set(self, key: str, value: T, ttl: float | None = None) -> None:
        now = time.monotonic()
        expires = self._expires_at(ttl)
        new_store = {k: v for k, v in self._store.items() if v[1] > now}
        new_store[key] = (value, expires)
        self._store = new_store

    def get(self, key: str, default: T | object = None) -> T | object:
        store = self._store
        item = store.get(key)
        if item is None:
            return default
        value, expires_at = item
        if expires_at <= time.monotonic():
            # Lazy cleanup on write/copy; expired item returns default
            return default
        return value

    def has(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def delete(self, key: str) -> None:
        if key in self._store:
            self._store = {k: v for k, v in self._store.items() if k != key}

    def clear(self) -> None:
        self._store = {}

    def get_or_set(self, key: str, factory: Callable[[], T], ttl: float | None = None) -> T:
        value = self.get(key, _MISSING)
        if value is not _MISSING:
            return value  # type: ignore[return-value]
        value = factory()
        if inspect.isawaitable(value):
            raise TypeError("factory returned awaitable; use get_or_set_async")
        self.set(key, value, ttl=ttl)
        return value

    async def get_or_set_async(self, key: str, factory: Callable[[], T | Awaitable[T]], ttl: float | None = None) -> T:
        value = self.get(key, _MISSING)
        if value is not _MISSING:
            return value  # type: ignore[return-value]
        value = factory()
        if inspect.isawaitable(value):
            value = await value
        self.set(key, value, ttl=ttl)
        return value


cache: TimeCache[object] = TimeCache()
