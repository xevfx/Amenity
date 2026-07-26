import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import core.amenity as amenity_module
import core.checks as checks_module
from cogs.reminder import Reminder
from core.amenity import Amenity


async def _assert_event_loop_stays_responsive(task: asyncio.Task[object]) -> None:
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.005)
    await task
    assert ticks >= 5


@pytest.mark.asyncio
async def test_check_initialization_runs_blocking_database_work_off_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def slow_refresh_cache() -> None:
        worker_threads.append(threading.get_ident())
        time.sleep(0.1)

    monkeypatch.setattr(checks_module, "refresh_cache", slow_refresh_cache)

    task = asyncio.create_task(checks_module.initialize_checks())
    await _assert_event_loop_stays_responsive(task)

    assert worker_threads
    assert worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_periodic_user_flush_does_not_block_gateway_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def slow_flush() -> int:
        worker_threads.append(threading.get_ident())
        time.sleep(0.1)
        return 0

    monkeypatch.setattr(amenity_module, "flush_pending_installed_users", slow_flush)

    task = asyncio.create_task(Amenity.flush_installed_users.coro(object()))
    await _assert_event_loop_stays_responsive(task)

    assert worker_threads
    assert worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_periodic_reminder_query_does_not_block_gateway_loop() -> None:
    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def slow_fetch_due_reminders(_now: int) -> list[dict]:
        worker_threads.append(threading.get_ident())
        time.sleep(0.1)
        return []

    reminder = object.__new__(Reminder)
    reminder._fetch_due_reminders = slow_fetch_due_reminders

    task = asyncio.create_task(Reminder.check_reminders.coro(reminder))
    await _assert_event_loop_stays_responsive(task)

    assert worker_threads
    assert worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_maintenance_loops_wait_for_the_startup_grace_period() -> None:
    class Bot:
        def __init__(self) -> None:
            self.waits = 0

        async def _wait_for_startup_maintenance(self) -> None:
            self.waits += 1

    bot = Bot()

    await Amenity.before_check_premium_expiry(bot)
    await Amenity.before_flush_installed_users(bot)

    reminder = object.__new__(Reminder)
    reminder.bot = bot
    await Reminder.check_reminders_before_loop(reminder)

    assert bot.waits == 3


@pytest.mark.asyncio
async def test_blocking_executor_is_configured_and_prewarmed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Loop:
        def __init__(self) -> None:
            self.executor: ThreadPoolExecutor | None = None
            self.calls: list[tuple[ThreadPoolExecutor | None, object]] = []

        def set_default_executor(self, executor: ThreadPoolExecutor) -> None:
            self.executor = executor

        async def _complete(self) -> None:
            return None

        def run_in_executor(self, executor: ThreadPoolExecutor | None, callback: object, *args: object) -> object:
            del args
            self.calls.append((executor, callback))
            return self._complete()

    loop = Loop()
    bot = object.__new__(Amenity)
    bot._blocking_executor = ThreadPoolExecutor(max_workers=amenity_module.BLOCKING_WORKER_COUNT)
    bot._blocking_executor_ready = False
    monkeypatch.setattr(amenity_module.asyncio, "get_running_loop", lambda: loop)

    try:
        await Amenity._prepare_blocking_executor(bot)
    finally:
        bot._blocking_executor.shutdown(wait=True)

    assert loop.executor is bot._blocking_executor
    assert len(loop.calls) == amenity_module.BLOCKING_WORKER_COUNT
    assert all(executor is None for executor, _callback in loop.calls)
    assert bot._blocking_executor_ready
