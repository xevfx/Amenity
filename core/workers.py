import asyncio
import contextvars
import functools
import threading
import weakref
from asyncio import AbstractEventLoop, Semaphore
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock
from typing import TypeVar

T = TypeVar("T")

CPU_WORKER_COUNT = 4
CPU_QUEUE_CAPACITY = 8
CPU_QUEUE_WAIT_TIMEOUT = 10.0
CPU_TASK_TIMEOUT = 30.0
_cpu_executor: ThreadPoolExecutor | None = None
_cpu_executor_lock = RLock()
_cpu_executor_shutdown = False
_cpu_slots_lock = RLock()
_cpu_slots_by_loop: weakref.WeakKeyDictionary[AbstractEventLoop, Semaphore] = weakref.WeakKeyDictionary()


def _get_cpu_executor() -> ThreadPoolExecutor:
    global _cpu_executor
    if _cpu_executor_shutdown:
        raise RuntimeError("CPU executor is shut down")
    with _cpu_executor_lock:
        if _cpu_executor_shutdown:
            raise RuntimeError("CPU executor is shut down")
        if _cpu_executor is None:
            _cpu_executor = ThreadPoolExecutor(
                max_workers=CPU_WORKER_COUNT,
                thread_name_prefix="amenity-cpu",
            )
        return _cpu_executor


def _submit_cpu[T](function: Callable[[], T]) -> Future[T]:
    with _cpu_executor_lock:
        return _get_cpu_executor().submit(function)


def _get_cpu_slots(loop: AbstractEventLoop) -> Semaphore:
    with _cpu_slots_lock:
        slots = _cpu_slots_by_loop.get(loop)
        if slots is None:
            slots = Semaphore(CPU_WORKER_COUNT + CPU_QUEUE_CAPACITY)
            _cpu_slots_by_loop[loop] = slots
        return slots


async def run_cpu[**P, T](function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    loop = asyncio.get_running_loop()
    slots = _get_cpu_slots(loop)
    try:
        await asyncio.wait_for(slots.acquire(), timeout=CPU_QUEUE_WAIT_TIMEOUT)
    except TimeoutError as exc:
        raise TimeoutError("CPU worker queue is full") from exc

    context = contextvars.copy_context()
    call = functools.partial(context.run, function, *args, **kwargs)
    try:
        future = _submit_cpu(call)
    except BaseException:
        slots.release()
        raise

    def release_slot(_: Future[T]) -> None:
        try:
            loop.call_soon_threadsafe(slots.release)
        except RuntimeError:
            # The event loop is already closed; its semaphore is no longer in use.
            pass

    future.add_done_callback(release_slot)
    wrapped = asyncio.wrap_future(future, loop=loop)
    try:
        return await asyncio.wait_for(asyncio.shield(wrapped), timeout=CPU_TASK_TIMEOUT)
    except (asyncio.CancelledError, TimeoutError):
        # A running Python thread cannot be force-stopped, but queued work can be
        # canceled and the caller never waits past the task deadline.
        future.cancel()
        raise


def _wait_for_cpu_worker_pool(barrier: threading.Barrier) -> None:
    barrier.wait(timeout=10)


async def prewarm_cpu_executor() -> None:
    global _cpu_executor_shutdown
    barrier = threading.Barrier(CPU_WORKER_COUNT)
    with _cpu_executor_lock:
        _cpu_executor_shutdown = False
        executor = _get_cpu_executor()
        futures = [executor.submit(_wait_for_cpu_worker_pool, barrier) for _ in range(CPU_WORKER_COUNT)]
    try:
        await asyncio.gather(*(asyncio.wrap_future(future) for future in futures))
    except threading.BrokenBarrierError as exc:
        raise RuntimeError("Could not start CPU workers before opening the Discord gateway.") from exc


def shutdown_cpu_executor(*, wait: bool = False) -> None:
    global _cpu_executor, _cpu_executor_shutdown
    with _cpu_executor_lock:
        executor = _cpu_executor
        _cpu_executor = None
        _cpu_executor_shutdown = True
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)
