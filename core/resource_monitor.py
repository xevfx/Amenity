"""Low-overhead process resource sampling with command and stack attribution."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from typing import TYPE_CHECKING

from api.log import log_resource_alert

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 1.0
DEFAULT_CPU_SPIKE_PERCENT = 150.0
DEFAULT_CPU_HIGH_PERCENT = 85.0
DEFAULT_CPU_HIGH_SECONDS = 5.0
DEFAULT_RSS_LIMIT_MB = 1024.0
DEFAULT_RSS_SPIKE_MB = 300.0
DEFAULT_ALERT_COOLDOWN_SECONDS = 300.0
STACKS_IN_ALERT = 5


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s value; using %.1f.", name, default)
        return default


def _process_rss_bytes() -> int:
    """Return current resident memory, with a portable peak-memory fallback."""
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            resident_pages = int(statm.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError, AttributeError):
        try:
            import resource

            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(peak_rss if sys.platform == "darwin" else peak_rss * 1024)
        except (ImportError, OSError, AttributeError):
            return 0


class ResourceMonitor:
    """Sample CPU/RSS off-loop and send rate-limited error webhook alerts."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = str(project_root.resolve()) + os.sep
        self._monitor_file = os.path.abspath(__file__)
        self._load_settings()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: AbstractEventLoop | None = None
        self._commands_lock = threading.Lock()
        self._active_commands: Counter[str] = Counter()
        self._recent_commands: deque[tuple[float, str]] = deque(maxlen=20)
        self._last_alert_at: dict[str, float] = {}
        self._cpu_high_since: float | None = None

    def _load_settings(self) -> None:
        self._interval = _env_float("AMENITY_RESOURCE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
        self._cpu_spike_percent = _env_float(
            "AMENITY_RESOURCE_CPU_SPIKE_PERCENT",
            DEFAULT_CPU_SPIKE_PERCENT,
        )
        self._cpu_high_percent = _env_float(
            "AMENITY_RESOURCE_CPU_HIGH_PERCENT",
            DEFAULT_CPU_HIGH_PERCENT,
        )
        self._cpu_high_seconds = _env_float(
            "AMENITY_RESOURCE_CPU_HIGH_SECONDS",
            DEFAULT_CPU_HIGH_SECONDS,
        )
        self._rss_limit_bytes = int(
            _env_float("AMENITY_RESOURCE_RSS_LIMIT_MB", DEFAULT_RSS_LIMIT_MB) * 1024 * 1024
        )
        self._rss_spike_bytes = int(
            _env_float("AMENITY_RESOURCE_RSS_SPIKE_MB", DEFAULT_RSS_SPIKE_MB) * 1024 * 1024
        )
        self._cooldown_seconds = _env_float(
            "AMENITY_RESOURCE_ALERT_COOLDOWN_SECONDS",
            DEFAULT_ALERT_COOLDOWN_SECONDS,
        )

    def start(self, loop: AbstractEventLoop) -> None:
        if self._thread is not None:
            return
        # The bot loads .env during startup, after its instance is constructed.
        self._load_settings()
        self._loop = loop
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._sample_resources,
            name="amenity-resource-monitor",
            daemon=True,
        )
        self._thread.start()
        if not os.getenv("ERROR_HOOK"):
            logger.warning("Resource monitoring is active, but ERROR_HOOK is not configured.")

    async def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, self._interval + 1.0)
            self._thread = None
        self._loop = None

    @contextmanager
    def track_command(self, command_name: str) -> Iterator[None]:
        with self._commands_lock:
            self._active_commands[command_name] += 1
        try:
            yield
        finally:
            with self._commands_lock:
                self._active_commands[command_name] -= 1
                if self._active_commands[command_name] <= 0:
                    del self._active_commands[command_name]
                self._recent_commands.append((time.monotonic(), command_name))

    def _command_names(self, now: float) -> tuple[list[str], list[str]]:
        with self._commands_lock:
            while self._recent_commands and now - self._recent_commands[0][0] > 10:
                self._recent_commands.popleft()
            active_commands = list(self._active_commands)
            recent_commands = list(dict.fromkeys(
                name for _, name in self._recent_commands
            ))
            return active_commands, recent_commands

    def _sample_resources(self) -> None:
        previous_cpu = time.process_time()
        previous_time = time.monotonic()
        previous_rss = _process_rss_bytes()

        while not self._stop.wait(self._interval):
            try:
                now = time.monotonic()
                elapsed = max(now - previous_time, 0.001)
                cpu_time = time.process_time()
                cpu_percent = max(0.0, (cpu_time - previous_cpu) / elapsed * 100)
                rss = _process_rss_bytes()
                stacks = self._sample_python_stacks()

                self._check_cpu(cpu_percent, now, rss, stacks)
                self._check_rss(rss, previous_rss, now, cpu_percent, stacks)

                previous_cpu = cpu_time
                previous_time = now
                previous_rss = rss
            except Exception:
                logger.exception("Resource monitor sampling failed; continuing to monitor.")

    def _sample_python_stacks(self) -> list[tuple[str, str]]:
        current_frames = sys._current_frames()
        samples: list[tuple[str, str]] = []
        for thread in threading.enumerate():
            if thread.ident is None or thread.name == "amenity-resource-monitor":
                continue
            frame = current_frames.get(thread.ident)
            project_frames: list[str] = []
            while frame is not None:
                filename = os.path.abspath(frame.f_code.co_filename)
                if filename.startswith(self._project_root) and filename != self._monitor_file:
                    relative_filename = os.path.relpath(filename, self._project_root)
                    project_frames.append(
                        f"{relative_filename}:{frame.f_lineno} in {frame.f_code.co_name}()"
                    )
                frame = frame.f_back

            if project_frames:
                # Frames are collected from the currently running function outward.
                stack = "\n".join(project_frames[:5])
                samples.append((thread.name, stack))
        return samples

    def _check_cpu(
        self,
        cpu_percent: float,
        now: float,
        rss: int,
        stacks: list[tuple[str, str]],
    ) -> None:
        if cpu_percent >= self._cpu_high_percent:
            if self._cpu_high_since is None:
                self._cpu_high_since = now
        else:
            self._cpu_high_since = None

        if cpu_percent >= self._cpu_spike_percent:
            self._report(
                "cpu",
                "CPU spike detected",
                f"Process CPU reached **{cpu_percent:.0f}%** for this sample "
                f"(one fully used core is 100%).",
                cpu_percent,
                rss,
                stacks,
                now,
            )
        elif (
            self._cpu_high_since is not None
            and now - self._cpu_high_since >= self._cpu_high_seconds
        ):
            self._report(
                "cpu",
                "Sustained high CPU usage",
                f"Process CPU stayed at or above **{self._cpu_high_percent:.0f}%** "
                f"for {now - self._cpu_high_since:.0f} seconds; the latest sample was "
                f"**{cpu_percent:.0f}%**.",
                cpu_percent,
                rss,
                stacks,
                now,
            )

    def _check_rss(
        self,
        rss: int,
        previous_rss: int,
        now: float,
        cpu_percent: float,
        stacks: list[tuple[str, str]],
    ) -> None:
        if rss <= 0:
            return
        if rss >= self._rss_limit_bytes:
            reason = (
                f"Resident memory reached **{rss / 1024 / 1024:.0f} MiB**, above the "
                f"configured **{self._rss_limit_bytes / 1024 / 1024:.0f} MiB** limit."
            )
            self._report("rss-high", "High memory usage", reason, cpu_percent, rss, stacks, now)
        elif rss - previous_rss >= self._rss_spike_bytes:
            reason = (
                f"Resident memory increased by **{(rss - previous_rss) / 1024 / 1024:.0f} MiB** "
                f"in about {self._interval:.0f} second(s). Current usage is "
                f"**{rss / 1024 / 1024:.0f} MiB**."
            )
            self._report("rss-spike", "Memory spike detected", reason, cpu_percent, rss, stacks, now)

    def _report(
        self,
        key: str,
        title: str,
        reason: str,
        cpu_percent: float,
        rss: int,
        stacks: list[tuple[str, str]],
        now: float,
    ) -> None:
        last_alert = self._last_alert_at.get(key)
        if last_alert is not None and now - last_alert < self._cooldown_seconds:
            return
        self._last_alert_at[key] = now

        active_commands, recent_commands = self._command_names(now)
        command_summary = ", ".join(f"`{name}`" for name in active_commands[:10]) or "None detected"
        recent_summary = ", ".join(f"`{name}`" for name in recent_commands[:10]) or "None detected"
        stack_summary = "\n\n".join(
            f"**{thread_name}**\n```\n{stack}\n```"
            for thread_name, stack in stacks[:STACKS_IN_ALERT]
        ) or "No project Python frame was captured in the sample."
        description = (
            f"{reason}\n\n"
            f"**Active command(s):** {command_summary}\n"
            f"**Recently completed command(s, last 10s):** {recent_summary}\n"
            f"**Process CPU:** {cpu_percent:.0f}%\n"
            f"**Resident memory:** {rss / 1024 / 1024:.0f} MiB\n\n"
            f"**Sampled active Python stacks:**\n{stack_summary}"
        )
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.error("Resource alert could not be sent because the bot event loop is closed: %s", title)
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                log_resource_alert(title, description),
                loop,
            )
            future.add_done_callback(self._log_send_failure)
        except RuntimeError:
            logger.exception("Could not schedule resource alert webhook: %s", title)

    @staticmethod
    def _log_send_failure(future: object) -> None:
        try:
            future.result()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Could not send resource alert webhook.")
