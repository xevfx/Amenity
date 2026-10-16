import asyncio
import json
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

import core.checks as checks
from cogs.tools import Tools
from core.amenity import Amenity
from core.cache import TimeCache
from core.workers import run_cpu, shutdown_cpu_executor


def _tavily_writer(path_value: str, credits: int) -> None:
    import cogs.tools as tools_module

    path = Path(path_value)
    tools_module.TAVILY_USAGE_PATH = path
    tools_module.TAVILY_USAGE_LOCK_PATH = path.with_suffix(".lock")
    for _ in range(credits):
        Tools._record_tavily_credits_sync(1)


def test_time_cache_get_or_set_is_single_flight() -> None:
    cache = TimeCache(default_ttl=60)
    release = threading.Event()
    started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def factory() -> int:
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=2)
        return 42

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(cache.get_or_set, "key", factory) for _ in range(8)]
        assert started.wait(timeout=2)
        release.set()
        results = [future.result(timeout=2) for future in futures]

    assert results == [42] * 8
    assert calls == 1


@pytest.mark.asyncio
async def test_time_cache_async_get_or_set_is_single_flight() -> None:
    cache = TimeCache(default_ttl=60)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 42

    tasks = [asyncio.create_task(cache.get_or_set_async("key", factory)) for _ in range(8)]
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert results == [42] * 8
    assert calls == 1


def test_time_cache_preserves_concurrent_writes() -> None:
    cache = TimeCache(default_ttl=60)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda value: cache.set(f"key-{value}", value), range(200)))

    assert len(cache) == 200


def test_time_cache_prune_returns_a_copy() -> None:
    cache: TimeCache[str] = TimeCache(default_ttl=60)
    cache.set("key", "value")

    exposed = cache.prune()
    exposed["injected"] = ("value", 9999999999.0)

    assert cache.get("injected") is None
    assert len(cache) == 1


@pytest.mark.asyncio
async def test_bot_offloaded_work_uses_a_bounded_worker_pool() -> None:
    bot = Amenity()
    loop = asyncio.get_running_loop()
    loop.set_default_executor(bot._blocking_executor)
    active = 0
    maximum_active = 0
    active_lock = threading.Lock()

    def blocking_work() -> None:
        nonlocal active, maximum_active
        with active_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with active_lock:
            active -= 1

    async def work() -> None:
        await asyncio.to_thread(blocking_work)

    try:
        await asyncio.gather(*(work() for _ in range(8)))
    finally:
        bot._blocking_executor.shutdown(wait=True)

    assert maximum_active == 4


@pytest.mark.asyncio
async def test_cpu_work_does_not_block_default_offloaded_work() -> None:
    bot = Amenity()
    loop = asyncio.get_running_loop()
    loop.set_default_executor(bot._blocking_executor)
    started = threading.Event()
    release = threading.Event()

    def cpu_work() -> None:
        started.set()
        assert release.wait(timeout=2)

    cpu_tasks = [asyncio.create_task(run_cpu(cpu_work)) for _ in range(2)]
    try:
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.wait_for(asyncio.to_thread(lambda: None), timeout=1)
    finally:
        release.set()
        await asyncio.gather(*cpu_tasks)
        shutdown_cpu_executor(wait=True)
        bot._blocking_executor.shutdown(wait=True)


def test_premium_updates_are_serialized_with_cache_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks, "DB_PATH", tmp_path / "checks.db")
    monkeypatch.setattr(checks, "KEYS_DB_PATH", tmp_path / "keys.db")
    monkeypatch.setattr(checks, "_initialized", False)
    checks._blacklisted_users.clear()
    checks._disabled_commands.clear()
    checks._premium_users.clear()
    checks.init_db()

    def add_premium(_: int) -> int:
        return checks.add_premium(123, 60)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(add_premium, range(8)))

    assert checks.get_premium_expires_at(123) is not None
    assert checks.get_premium_expires_at(123) >= checks._now() + 7 * 60


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


def test_tavily_usage_updates_are_process_safe(tmp_path: Path) -> None:
    path = tmp_path / "tavily_usage.json"
    with ProcessPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_tavily_writer, str(path), 10) for _ in range(4)]
        for future in futures:
            future.result(timeout=10)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["used"] == 40
    assert data["search_usage"] == 40
    assert data["remaining"] == 960
