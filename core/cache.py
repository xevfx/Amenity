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

    def _expires_at(self, ttl: float | None) -> float:
        if ttl is None:
            ttl = self.default_ttl
        return time.monotonic() + float(ttl)

    def set(self, key: str, value: T, ttl: float | None = None) -> None:
        self._store[key] = (value, self._expires_at(ttl))

    def get(self, key: str, default: T | object = None) -> T | object:
        item = self._store.get(key)
        if not item:
            return default
        value, expires_at = item
        if expires_at <= time.monotonic():
            self._store.pop(key, None)
            return default
        return value

    def has(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

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
